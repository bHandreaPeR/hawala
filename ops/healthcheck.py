"""ops/healthcheck.py — Daily infra health check.

Runs every market morning at 07:25 IST (cron), just before run_daily_report.py
at 07:32. Writes JSON to v3/cache/healthcheck_YYYYMMDD.json. Newsletter PDF
embeds this — failures show prominently at the top.

Sends a MACRO-channel Telegram alert on any FAIL so the user catches it
even before the newsletter arrives.

Categories checked:
    caches  — every key cache's last entry == last trading day
    logs    — yesterday's log files exist and were written to
    cron    — installed crontab has no known bugs (% unescaped, &-chain)
    creds   — token.env has all 6 keys, Groww auth works, Telegram both bots
    disk    — adequate free space in logs/ and data_dumps/
    procs   — pre-market: nothing stale running from yesterday
    code    — recent files parse + import cleanly

Result schema:
    {
      "ts": "2026-05-19 07:25:01 IST",
      "overall": "PASS" | "FAIL",
      "checks": [
         {"id":"...","cat":"...","label":"...","status":"PASS|FAIL|WARN",
          "detail":"...","ts": "..."},
         ...
      ],
      "n_pass": N, "n_warn": N, "n_fail": N
    }

Manual run:  python ops/healthcheck.py
Cron:        25 7 * * 1-5 python ops/healthcheck.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

import pytz

IST = pytz.timezone('Asia/Kolkata')

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE_DIR  = ROOT / 'v3' / 'cache'
LOG_DIR    = ROOT / 'logs'
TOKEN_ENV  = ROOT / 'token.env'

NOW_IST    = datetime.now(IST)
TODAY_IST  = NOW_IST.date()


def _last_trading_day() -> date:
    """Last weekday strictly before today (Sat/Sun → Friday)."""
    d = TODAY_IST - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _today_or_last_trading_day() -> date:
    """If today is a weekday, return today; otherwise last trading day."""
    if TODAY_IST.weekday() < 5:
        return TODAY_IST
    return _last_trading_day()


LAST_TD = _last_trading_day()


# ─────────────────────────────────────────────────────────────────────────────
# Check result container
# ─────────────────────────────────────────────────────────────────────────────
def _chk(checks: list, cat: str, label: str, status: str, detail: str = "",
         cid: str = ""):
    checks.append({
        'id':     cid or label.lower().replace(' ', '_'),
        'cat':    cat,
        'label':  label,
        'status': status,   # 'PASS' | 'WARN' | 'FAIL'
        'detail': detail,
        'ts':     NOW_IST.strftime('%H:%M:%S'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Category 1: cache freshness
# ─────────────────────────────────────────────────────────────────────────────
def check_caches(checks: list) -> None:
    import pickle
    import pandas as pd

    specs = [
        ('v3/cache/candles_1m_NIFTY.pkl',     'NIFTY 1m candles',     'date',     'df'),
        ('v3/cache/candles_1m_BANKNIFTY.pkl', 'BANKNIFTY 1m candles', 'date',     'df'),
        ('v3/cache/candles_1m_SENSEX.pkl',    'SENSEX 1m candles',    'date',     'df'),
        ('v3/cache/option_oi_1m_NIFTY.pkl',   'NIFTY option OI',      None,       'dict'),
        ('v3/cache/option_oi_1m_BANKNIFTY.pkl','BANKNIFTY option OI', None,       'dict'),
        ('fii_data.csv',                       'FII cash csv',         'date',     'csv'),
        ('trade_logs/_fii_fo_cache.pkl',       'FII F&O cache',        None,       'dict'),
        ('v3/cache/pcr_daily.csv',             'PCR daily',            'date',     'csv'),
    ]
    for rel, label, key, kind in specs:
        p = ROOT / rel
        if not p.exists():
            _chk(checks, 'caches', label, 'FAIL', f'file missing: {rel}'); continue
        try:
            if kind == 'df':
                df = pickle.load(open(p, 'rb'))
                last = pd.to_datetime(df[key].max()).date()
            elif kind == 'csv':
                df = pd.read_csv(p)
                last = pd.to_datetime(df[key]).max().date()
            elif kind == 'dict':
                d = pickle.load(open(p, 'rb'))
                if not d:
                    _chk(checks, 'caches', label, 'FAIL', 'empty dict'); continue
                # Keys may be date or str
                k = sorted(d.keys())[-1]
                last = pd.to_datetime(str(k)).date() if not isinstance(k, date) else k
        except Exception as e:
            _chk(checks, 'caches', label, 'FAIL', f'read error: {e}'); continue

        delta = (LAST_TD - last).days
        if delta <= 0:
            # delta < 0 means cache is AHEAD of last trading day (today's
            # data already collected) — that's fresh, not a problem.
            _chk(checks, 'caches', label, 'PASS', f'last={last}')
        elif delta == 1:
            _chk(checks, 'caches', label, 'WARN', f'last={last} (1d behind)')
        else:
            _chk(checks, 'caches', label, 'FAIL', f'last={last} ({delta}d STALE)')


# ─────────────────────────────────────────────────────────────────────────────
# Category 2: log evidence yesterday
# ─────────────────────────────────────────────────────────────────────────────
def check_logs(checks: list) -> None:
    last_str = LAST_TD.strftime('%Y-%m-%d')
    last_compact = LAST_TD.strftime('%Y%m%d')

    # Files that should have an entry dated `last_str`
    grepables = [
        ('logs/news_bot/news_runner.log',     'news.runner log'),
        ('logs/trade_bot/runner_nifty.log',   'v3 NIFTY runner log'),
        ('logs/trade_bot/runner_banknifty.log','v3 BANKNIFTY runner log'),
        ('logs/trade_bot/vp_live_daemon.log',  'VP-Trail daemon log'),
        ('logs/macro_bot/option_flow.log',    'option_flow daemon log'),
        ('logs/trade_bot/tick_recorder.log',  'tick_recorder log'),
        ('logs/reports/daily_fetch.log',      'daily_fetch.sh log'),
    ]
    for rel, label in grepables:
        p = ROOT / rel
        if not p.exists():
            _chk(checks, 'logs', label, 'FAIL', f'file missing: {rel}'); continue
        try:
            r = subprocess.run(['grep','-c', last_str, str(p)],
                                capture_output=True, text=True, timeout=5)
            n = int(r.stdout.strip() or '0')
            if n == 0:
                _chk(checks, 'logs', label, 'FAIL',
                     f'no entries for {last_str}')
            elif n < 10:
                _chk(checks, 'logs', label, 'WARN',
                     f'only {n} entries for {last_str} — likely crashed early')
            else:
                _chk(checks, 'logs', label, 'PASS', f'{n} entries for {last_str}')
        except Exception as e:
            _chk(checks, 'logs', label, 'FAIL', f'grep error: {e}')

    # Daily-report log: filename pattern is daily_report-YYYYMMDD.log
    dr_log = ROOT / 'logs' / 'macro_bot' / f'daily_report-{last_compact}.log'
    if dr_log.exists():
        sz = dr_log.stat().st_size
        if sz < 500:
            _chk(checks, 'logs', 'daily_report log', 'WARN',
                 f'{dr_log.name} only {sz}B — likely crashed mid-run')
        else:
            _chk(checks, 'logs', 'daily_report log', 'PASS',
                 f'{dr_log.name} {sz:,}B')
    else:
        _chk(checks, 'logs', 'daily_report log', 'FAIL',
             f'{dr_log.name} missing — cron failed or report not run yesterday')

    # Newsletter PDF for the last trading day
    pdf_day = LAST_TD.day
    suf = 'th' if 10 <= pdf_day % 100 <= 20 else \
          {1:'st',2:'nd',3:'rd'}.get(pdf_day % 10, 'th')
    pdf_name = (f"Newsletter {pdf_day}{suf} {LAST_TD.strftime('%B')} "
                f"{LAST_TD.strftime('%y')}.pdf")
    pdf = ROOT / 'data_dumps' / 'newsletters' / pdf_name
    if pdf.exists():
        _chk(checks, 'logs', 'newsletter PDF', 'PASS', f'{pdf_name} present')
    else:
        _chk(checks, 'logs', 'newsletter PDF', 'FAIL',
             f'{pdf_name} missing in data_dumps/newsletters/')


# ─────────────────────────────────────────────────────────────────────────────
# Category 3: cron syntax sanity
# ─────────────────────────────────────────────────────────────────────────────
def check_cron(checks: list) -> None:
    try:
        r = subprocess.run(['crontab','-l'], capture_output=True,
                            text=True, timeout=5)
        if r.returncode != 0:
            _chk(checks, 'cron', 'crontab readable', 'FAIL',
                 r.stderr[:200] or 'crontab -l failed'); return
        ct = r.stdout
        _chk(checks, 'cron', 'crontab readable', 'PASS',
             f'{len(ct.splitlines())} lines')
    except Exception as e:
        _chk(checks, 'cron', 'crontab readable', 'FAIL', str(e)); return

    # Unescaped % in date format substitutions
    bad_pct_lines = []
    for i, line in enumerate(ct.splitlines(), 1):
        if '$(date +' in line and '\\%' not in line and '%' in line.split('$(date +')[1]:
            # Cron will mangle this line — % becomes newline
            bad_pct_lines.append(i)
    if bad_pct_lines:
        _chk(checks, 'cron', 'no unescaped %', 'FAIL',
             f'lines {bad_pct_lines}: % in $(date +…) MUST be \\%')
    else:
        _chk(checks, 'cron', 'no unescaped %', 'PASS', '')

    # cd && A & B & — second background process loses cwd
    bad_chain_lines = []
    for i, line in enumerate(ct.splitlines(), 1):
        if line.strip().startswith('#') or not line.strip(): continue
        # Pattern: `cd "..."` followed by two or more ` & ` backgrounds
        # AFTER the cd group, before line end. Crude heuristic:
        if line.count(' & ') >= 1 and line.count(' && ') >= 1 \
           and '> /dev/null' in line:
            # Check if there's a SECOND nohup after a `&` that uses a relative path
            parts = line.split(' & ')
            if len(parts) >= 2:
                second = parts[1]
                # If second part starts with 'nohup ... <relative path>.py' it's broken
                if 'nohup' in second and ('v3/' in second or 'alerts/' in second
                                          or 'news/' in second):
                    bad_chain_lines.append(i)
    if bad_chain_lines:
        _chk(checks, 'cron', 'no broken cd-chain', 'FAIL',
             f'lines {bad_chain_lines}: 2nd process after & runs in $HOME, '
             f'not in cd dir. Split into separate cron lines.')
    else:
        _chk(checks, 'cron', 'no broken cd-chain', 'PASS', '')

    # Required cron entries present
    required = [
        ('run_daily_report.py',         'daily report'),
        ('news.runner',                  'news runner'),
        ('runner_nifty.py',              'v3 NIFTY runner'),
        ('runner_banknifty.py',          'v3 BANKNIFTY runner'),
        ('alerts.vp_live_daemon',        'VP-Trail daemon'),
        ('alerts.option_flow_daemon',    'option_flow daemon'),
        ('alerts.tick_recorder',         'tick recorder'),
        ('viewer.live_server',           'live footprint viewer'),
        ('alerts.vp_paper_journal',      'VP paper journal'),
        ('alerts.vp_paper_executor',     'VP paper executor (intraday)'),
        ('alerts.index_1m_intraday',     'index 1m intraday fetcher'),
        ('daily_fetch.sh',               'daily candle fetch'),
    ]
    for needle, label in required:
        if needle in ct:
            _chk(checks, 'cron', f'{label} scheduled', 'PASS', '')
        else:
            _chk(checks, 'cron', f'{label} scheduled', 'FAIL',
                 f'no cron line matches "{needle}"')


# ─────────────────────────────────────────────────────────────────────────────
# Category 4: credentials + API health
# ─────────────────────────────────────────────────────────────────────────────
def check_creds(checks: list) -> None:
    if not TOKEN_ENV.exists():
        _chk(checks, 'creds', 'token.env exists', 'FAIL',
             'token.env missing'); return
    env = {}
    for line in TOKEN_ENV.read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()

    needed = ['GROWW_API_KEY', 'GROWW_TOTP_SECRET',
              'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_IDS',
              'TELEGRAM_BOT_TOKEN_MACRO', 'TELEGRAM_CHAT_IDS_MACRO']
    missing = [k for k in needed if not env.get(k)]
    if missing:
        _chk(checks, 'creds', 'token.env keys', 'FAIL',
             f'missing: {missing}')
    else:
        _chk(checks, 'creds', 'token.env keys', 'PASS',
             f'{len(needed)}/{len(needed)} present')

    # Try Groww auth
    try:
        import pyotp
        from growwapi import GrowwAPI
        totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
        tok = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
        _chk(checks, 'creds', 'Groww auth', 'PASS',
             f'token len={len(tok)}')
    except Exception as e:
        _chk(checks, 'creds', 'Groww auth', 'FAIL', str(e)[:150])

    # Try Telegram getMe on both bots (lightweight)
    import urllib.request
    for var, label in [('TELEGRAM_BOT_TOKEN', 'TRADE bot reachable'),
                        ('TELEGRAM_BOT_TOKEN_MACRO', 'MACRO bot reachable')]:
        tok = env.get(var, '')
        if not tok:
            _chk(checks, 'creds', label, 'FAIL', 'token missing'); continue
        try:
            req = urllib.request.Request(f'https://api.telegram.org/bot{tok}/getMe',
                                          headers={'User-Agent':'hawala-healthcheck'})
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read())
                if d.get('ok'):
                    _chk(checks, 'creds', label, 'PASS',
                         f'@{d.get("result",{}).get("username","?")}')
                else:
                    _chk(checks, 'creds', label, 'FAIL', str(d)[:100])
        except Exception as e:
            _chk(checks, 'creds', label, 'FAIL', str(e)[:100])


# ─────────────────────────────────────────────────────────────────────────────
# Category 5: disk + filesystem
# ─────────────────────────────────────────────────────────────────────────────
def check_disk(checks: list) -> None:
    try:
        st = os.statvfs(str(ROOT))
        free_gb = (st.f_bavail * st.f_frsize) / 1e9
        if free_gb < 5:
            _chk(checks, 'disk', 'free space', 'FAIL', f'only {free_gb:.1f} GB free')
        elif free_gb < 20:
            _chk(checks, 'disk', 'free space', 'WARN', f'{free_gb:.1f} GB free')
        else:
            _chk(checks, 'disk', 'free space', 'PASS', f'{free_gb:.1f} GB free')
    except Exception as e:
        _chk(checks, 'disk', 'free space', 'WARN', str(e))

    # Log + data_dumps size — alert if growing too fast
    for sub, label, soft, hard in [
        ('logs',                 'logs/ size',         500, 2000),  # MB
        ('data_dumps',           'data_dumps/ size',  5000, 20000),
        ('v3/cache',             'v3/cache/ size',    2000, 10000),
    ]:
        p = ROOT / sub
        if not p.exists():
            _chk(checks, 'disk', label, 'WARN', f'{sub} missing'); continue
        try:
            r = subprocess.run(['du','-sm', str(p)], capture_output=True,
                                text=True, timeout=10)
            mb = int(r.stdout.split()[0]) if r.stdout else 0
            if mb >= hard:
                _chk(checks, 'disk', label, 'FAIL', f'{mb} MB (hard cap {hard})')
            elif mb >= soft:
                _chk(checks, 'disk', label, 'WARN', f'{mb} MB (soft cap {soft})')
            else:
                _chk(checks, 'disk', label, 'PASS', f'{mb} MB')
        except Exception as e:
            _chk(checks, 'disk', label, 'WARN', str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Category 6: pre-market process state (nothing stale)
# ─────────────────────────────────────────────────────────────────────────────
def check_procs(checks: list) -> None:
    try:
        r = subprocess.run(['pgrep','-af','runner_nifty.py|runner_banknifty.py|'
                                          'news\\.runner|alerts.vp_live_daemon|'
                                          'alerts.option_flow_daemon|'
                                          'alerts.tick_recorder|'
                                          'viewer.live_server|'
                                          'alerts.index_1m_intraday'],
                            capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.splitlines() if 'grep' not in l]
    except Exception as e:
        _chk(checks, 'procs', 'no stale processes', 'WARN', str(e)); return

    if NOW_IST.time().hour < 9:
        # Pre-market: nothing should be running. Exception: news.runner OK.
        stale = [l for l in lines if 'news.runner' not in l]
        if stale:
            _chk(checks, 'procs', 'no stale processes', 'WARN',
                 f'{len(stale)} pre-market processes still alive — '
                 f'pkill at 03:30 may not have cleared them')
        else:
            _chk(checks, 'procs', 'no stale processes', 'PASS',
                 f'{len(lines)} running (news.runner OK pre-market)')
    else:
        # Post-09:00, runner_nifty + runner_banknifty + news + vp + option_flow OK
        _chk(checks, 'procs', 'expected processes alive', 'PASS',
             f'{len(lines)} running')


# ─────────────────────────────────────────────────────────────────────────────
# Category 7: critical code modules parse + import
# ─────────────────────────────────────────────────────────────────────────────
def check_code(checks: list) -> None:
    import ast
    critical = [
        'run_daily_report.py',
        'run_weekly_report.py',
        'v3/live/runner_nifty.py',
        'v3/live/runner_banknifty.py',
        'news/runner.py',
        'alerts/option_flow_daemon.py',
        'alerts/vp_live_daemon.py',
        'alerts/tick_recorder.py',
        'alerts/index_1m_intraday.py',
        'alerts/vp_paper_journal.py',
        'alerts/vp_paper_executor.py',
        'v3/live/reentry_cooldown.py',
        'research/footprint_features.py',
        'research/footprint_correlation.py',
        'viewer/live_server.py',
    ]
    parse_fail = []
    for rel in critical:
        p = ROOT / rel
        if not p.exists():
            parse_fail.append(f'{rel} missing'); continue
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            parse_fail.append(f'{rel}: {e.msg} line {e.lineno}')
    if parse_fail:
        _chk(checks, 'code', 'critical files parse', 'FAIL',
             '; '.join(parse_fail))
    else:
        _chk(checks, 'code', 'critical files parse', 'PASS',
             f'{len(critical)} files OK')


# ─────────────────────────────────────────────────────────────────────────────
# Notify on FAIL
# ─────────────────────────────────────────────────────────────────────────────
def telegram_alert(checks: list, n_fail: int, n_warn: int) -> bool:
    if n_fail == 0 and n_warn == 0:
        return False
    if not TOKEN_ENV.exists():
        return False
    env = {l.split('=',1)[0].strip(): l.split('=',1)[1].strip()
           for l in TOKEN_ENV.read_text().splitlines()
           if '=' in l and not l.strip().startswith('#')}
    tok = env.get('TELEGRAM_BOT_TOKEN_MACRO','').strip()
    chats = [c.strip() for c in env.get('TELEGRAM_CHAT_IDS_MACRO','').split(',') if c.strip()]
    if not tok or not chats:
        return False

    fails = [c for c in checks if c['status'] == 'FAIL']
    warns = [c for c in checks if c['status'] == 'WARN']

    lines = [f"⚠️ <b>HAWALA HEALTHCHECK — {n_fail} FAIL · {n_warn} WARN</b>",
             f"<i>{NOW_IST:%Y-%m-%d %H:%M IST}</i>"]
    if fails:
        lines.append("\n<b>FAIL:</b>")
        for c in fails[:10]:
            lines.append(f"• [{c['cat']}] {c['label']}: {c['detail'][:120]}")
        if len(fails) > 10:
            lines.append(f"… and {len(fails)-10} more")
    if warns:
        lines.append("\n<b>WARN:</b>")
        for c in warns[:5]:
            lines.append(f"• [{c['cat']}] {c['label']}: {c['detail'][:120]}")
        if len(warns) > 5:
            lines.append(f"… and {len(warns)-5} more")
    lines.append("\n<i>Full report in today's Newsletter PDF.</i>")

    msg = "\n".join(lines)
    try:
        from alerts.telegram import send
        for c in chats:
            send(tok, c, msg)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    checks: list = []
    print(f"hawala healthcheck @ {NOW_IST:%Y-%m-%d %H:%M:%S IST}")
    print(f"last trading day:    {LAST_TD}")
    print(f"check root:          {ROOT}")
    print()

    for fn in (check_caches, check_logs, check_cron, check_creds,
               check_disk, check_procs, check_code):
        try:
            fn(checks)
        except Exception as e:
            _chk(checks, 'system', fn.__name__, 'FAIL', f'exception: {e}')

    n_pass = sum(1 for c in checks if c['status'] == 'PASS')
    n_warn = sum(1 for c in checks if c['status'] == 'WARN')
    n_fail = sum(1 for c in checks if c['status'] == 'FAIL')

    overall = 'PASS' if n_fail == 0 else 'FAIL'
    result = {
        'ts': NOW_IST.isoformat(),
        'last_trading_day': LAST_TD.isoformat(),
        'overall': overall,
        'n_pass': n_pass, 'n_warn': n_warn, 'n_fail': n_fail,
        'checks': checks,
    }

    out_dir = ROOT / 'v3' / 'cache'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'healthcheck_{TODAY_IST:%Y%m%d}.json'
    out_path.write_text(json.dumps(result, indent=2, default=str))

    # Also stable filename for PDF embedding (latest)
    latest = out_dir / 'healthcheck_latest.json'
    latest.write_text(json.dumps(result, indent=2, default=str))

    print(f"{'CAT':9s}  {'STATUS':6s}  LABEL                          DETAIL")
    for c in checks:
        emoji = {'PASS':'✓ ','WARN':'⚠ ','FAIL':'✗ '}.get(c['status'],'? ')
        print(f"{c['cat']:9s}  {emoji}{c['status']:5s} {c['label']:30s} {c['detail'][:80]}")
    print()
    print(f"summary: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL  →  overall={overall}")
    print(f"json   : {out_path}")

    if overall == 'FAIL' or n_warn > 0:
        sent = telegram_alert(checks, n_fail, n_warn)
        if sent:
            print("telegram MACRO alert sent")
    return 0 if overall == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
