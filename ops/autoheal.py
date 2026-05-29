"""ops/autoheal.py — Self-healing routine for the Hawala stack.

Runs every market morning at 06:55 IST (cron), BEFORE healthcheck (07:25)
and daily report (07:32). The sequence each morning is:

    06:55  autoheal.py   — detect failures, AUTO-FIX what it safely can,
                           re-verify, escalate the rest
    07:25  healthcheck.py — independent verification (should now be green)
    07:32  run_daily_report.py — Newsletter PDF embeds both results

Unlike healthcheck.py (detect-only), autoheal ACTS:
  - Stale data cache       → re-runs the corresponding fetcher
  - Stale PCR / bhavcopy   → re-runs the bhavcopy fetch
  - Stale FII data         → re-runs the FII fetchers

It does NOT auto-fix (too risky / needs a human):
  - Cron misconfiguration  (crontab edits)
  - Code parse errors      (needs a code change)
  - Auth failures          (credential issue)
  - Disk full              (needs manual cleanup)
These are escalated to the MACRO Telegram channel + flagged in the PDF.

Self-disabling intent: the routine keeps running daily. Once it records
N consecutive all-green mornings (GREEN_STREAK_TARGET), it logs a
"stack stable" note — the user can then relax the cadence if they wish.
It does not stop itself (a routine that stops can't catch regressions).

Output: v3/cache/autoheal_latest.json  (PDF reads this)
        v3/cache/autoheal_<YYYYMMDD>.json

Manual run:  python ops/autoheal.py
             python ops/autoheal.py --dry-run   (detect, don't fix)
Cron:        55 6 * * 1-5 python ops/autoheal.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

import pytz

IST  = pytz.timezone('Asia/Kolkata')
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PYTHON = sys.executable or '/opt/anaconda3/bin/python3'
NOW    = datetime.now(IST)
TODAY  = NOW.date()

GREEN_STREAK_TARGET = 5   # consecutive clean mornings = "stable"
FIX_TIMEOUT_SEC     = 300 # per fixer command


# ─────────────────────────────────────────────────────────────────────────────
# Fixer registry — maps a healthcheck failure label → remediation command(s).
# Each fixer is a list of (description, argv) run in order. Idempotent only.
# ─────────────────────────────────────────────────────────────────────────────
FIXERS: dict[str, list[tuple[str, list[str]]]] = {
    'NIFTY 1m candles': [
        ('fetch NIFTY 1m', [PYTHON, 'v3/data/fetch_1m_NIFTY.py'])],
    'BANKNIFTY 1m candles': [
        ('fetch BANKNIFTY 1m', [PYTHON, 'v3/data/fetch_1m_BANKNIFTY.py'])],
    'SENSEX 1m candles': [
        ('fetch SENSEX 1m', [PYTHON, 'v3/data/fetch_1m_SENSEX.py'])],
    'NIFTY option OI': [
        ('fetch NIFTY option OI', [PYTHON, 'v3/data/fetch_option_oi_NIFTY.py'])],
    'BANKNIFTY option OI': [
        ('fetch BANKNIFTY option OI', [PYTHON, 'v3/data/fetch_option_oi_BANKNIFTY.py'])],
    'FII cash csv': [
        ('fetch FII cash', [PYTHON, 'v3/data/fetch_fii_cash.py'])],
    'FII F&O cache': [
        ('fetch FII F&O', [PYTHON, 'v3/data/fetch_fii_fo.py'])],
    'PCR daily': [
        ('fetch NSE bhavcopy (NIFTY PCR)', [PYTHON, 'v3/data/fetch_bhavcopy_nifty.py']),
        ('fetch NSE bhavcopy (BANKNIFTY PCR)', [PYTHON, 'v3/data/fetch_bhavcopy_banknifty.py']),
        # fetch_bhavcopy_*.py only refresh the bhavcopy .pkl — they do NOT write
        # pcr_daily.csv (the file healthcheck monitors). build_pcr_daily.py is
        # the step that actually rebuilds the csv from the fresh pkl.
        ('rebuild pcr_daily.csv', [PYTHON, 'v3/data/build_pcr_daily.py'])],
}

# Failure categories autoheal will NOT touch — escalate only.
NO_AUTOFIX_CATS = {'cron', 'creds', 'code', 'disk'}


# ─────────────────────────────────────────────────────────────────────────────
def _run_healthcheck() -> dict:
    """Run ops/healthcheck.py and return its parsed JSON result."""
    try:
        subprocess.run([PYTHON, 'ops/healthcheck.py'],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=120)
    except Exception as e:
        return {'overall': 'FAIL', 'checks': [],
                'error': f'healthcheck run failed: {e}'}
    latest = ROOT / 'v3' / 'cache' / 'healthcheck_latest.json'
    if not latest.exists():
        return {'overall': 'FAIL', 'checks': [],
                'error': 'healthcheck produced no output'}
    return json.loads(latest.read_text())


def _run_fixer(desc: str, argv: list[str]) -> tuple[bool, str]:
    """Run one fixer command. Returns (success, detail)."""
    try:
        r = subprocess.run(argv, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=FIX_TIMEOUT_SEC)
        if r.returncode == 0:
            tail = (r.stdout.strip().splitlines() or [''])[-1][:120]
            return True, f'{desc}: OK — {tail}'
        return False, f'{desc}: exit {r.returncode} — {r.stderr.strip()[:150]}'
    except subprocess.TimeoutExpired:
        return False, f'{desc}: TIMEOUT after {FIX_TIMEOUT_SEC}s'
    except Exception as e:
        return False, f'{desc}: {type(e).__name__}: {e}'


def _green_streak() -> int:
    """Read prior autoheal results, count consecutive clean mornings."""
    streak = 0
    d = TODAY - timedelta(days=1)
    for _ in range(30):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        f = ROOT / 'v3' / 'cache' / f'autoheal_{d:%Y%m%d}.json'
        if not f.exists():
            break
        try:
            data = json.loads(f.read_text())
        except Exception:
            break
        if data.get('post_fix_overall') == 'PASS' and not data.get('unfixed'):
            streak += 1
            d -= timedelta(days=1)
        else:
            break
    return streak


# ─────────────────────────────────────────────────────────────────────────────
def _telegram(msg: str) -> bool:
    env_f = ROOT / 'token.env'
    if not env_f.exists():
        return False
    env = {l.split('=',1)[0].strip(): l.split('=',1)[1].strip()
           for l in env_f.read_text().splitlines()
           if '=' in l and not l.strip().startswith('#')}
    # Prefer the dedicated SANITY bot; fall back to MACRO if not yet configured.
    tok = (env.get('TELEGRAM_BOT_TOKEN_SANITY','').strip()
           or env.get('TELEGRAM_BOT_TOKEN_MACRO','').strip())
    raw = (env.get('TELEGRAM_CHAT_IDS_SANITY','').strip()
           or env.get('TELEGRAM_CHAT_IDS_MACRO','').strip())
    chats = [c.strip() for c in raw.split(',') if c.strip()]
    if not tok or not chats:
        return False
    try:
        from alerts.telegram import send
        for c in chats:
            send(tok, c, msg)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='detect + report what WOULD be fixed, but do not fix')
    args = ap.parse_args()

    print(f"autoheal @ {NOW:%Y-%m-%d %H:%M:%S IST}  dry_run={args.dry_run}")

    # ── Pass 1: detect ────────────────────────────────────────────────────
    hc1 = _run_healthcheck()
    checks1 = hc1.get('checks', [])
    problems = [c for c in checks1 if c['status'] in ('FAIL', 'WARN')]
    print(f"  initial: {hc1.get('overall')} — {len(problems)} problems")

    fixed, failed_to_fix, unfixable = [], [], []

    # ── Pass 2: auto-fix what we can ──────────────────────────────────────
    for c in problems:
        label, cat = c['label'], c['cat']
        if cat in NO_AUTOFIX_CATS:
            unfixable.append({'label': label, 'cat': cat, 'detail': c['detail']})
            print(f"  ESCALATE [{cat}] {label}: {c['detail'][:80]}")
            continue
        fixer = FIXERS.get(label)
        if not fixer:
            unfixable.append({'label': label, 'cat': cat,
                              'detail': c['detail'] + ' (no fixer registered)'})
            print(f"  NO FIXER  [{cat}] {label}")
            continue
        if args.dry_run:
            print(f"  WOULD FIX [{cat}] {label} via {[f[0] for f in fixer]}")
            fixed.append({'label': label, 'dry_run': True})
            continue
        # Run the fixer chain
        print(f"  FIXING    [{cat}] {label} ...")
        ok_all, details = True, []
        for desc, argv in fixer:
            ok, detail = _run_fixer(desc, argv)
            details.append(detail)
            print(f"    {'✓' if ok else '✗'} {detail}")
            if not ok:
                ok_all = False
        (fixed if ok_all else failed_to_fix).append(
            {'label': label, 'cat': cat, 'steps': details})

    # ── Pass 3: re-verify ─────────────────────────────────────────────────
    post_overall = hc1.get('overall')
    post_problems = problems
    if fixed and not args.dry_run:
        time.sleep(2)
        hc2 = _run_healthcheck()
        post_overall = hc2.get('overall')
        post_problems = [c for c in hc2.get('checks', [])
                         if c['status'] in ('FAIL', 'WARN')]
        print(f"  post-fix: {post_overall} — {len(post_problems)} problems remain")

    unfixed = [{'label': c['label'], 'cat': c['cat'], 'detail': c['detail']}
               for c in post_problems]

    streak = _green_streak()
    if post_overall == 'PASS' and not unfixed:
        streak += 1   # today counts

    result = {
        'ts': NOW.isoformat(),
        'dry_run': args.dry_run,
        'pre_fix_overall':  hc1.get('overall'),
        'post_fix_overall': post_overall,
        'fixed':       fixed,
        'failed_to_fix': failed_to_fix,
        'unfixable':   unfixable,
        'unfixed':     unfixed,
        'green_streak': streak,
        'stable': streak >= GREEN_STREAK_TARGET,
    }

    out_dir = ROOT / 'v3' / 'cache'
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f'autoheal_{TODAY:%Y%m%d}.json').write_text(
        json.dumps(result, indent=2, default=str))
    (out_dir / 'autoheal_latest.json').write_text(
        json.dumps(result, indent=2, default=str))

    # ── Notify ────────────────────────────────────────────────────────────
    n_fixed = len([f for f in fixed if not f.get('dry_run')])
    needs_human = failed_to_fix + unfixable
    if needs_human or n_fixed:
        lines = [f"🔧 <b>HAWALA AUTOHEAL — {NOW:%d-%b %H:%M}</b>"]
        if n_fixed:
            lines.append(f"\n<b>Auto-fixed ({n_fixed}):</b>")
            for f in fixed:
                if not f.get('dry_run'):
                    lines.append(f"• {f['label']}")
        if failed_to_fix:
            lines.append(f"\n<b>Fix FAILED ({len(failed_to_fix)}) — needs human:</b>")
            for f in failed_to_fix:
                lines.append(f"• {f['label']}")
        if unfixable:
            lines.append(f"\n<b>Cannot auto-fix ({len(unfixable)}) — needs human:</b>")
            for u in unfixable:
                lines.append(f"• [{u['cat']}] {u['label']}: {u['detail'][:90]}")
        lines.append(f"\nPost-fix: <b>{post_overall}</b> · "
                     f"green streak: {streak}"
                     + ("  ✅ STABLE" if result['stable'] else ""))
        _telegram("\n".join(lines))

    # ── Console summary ──────────────────────────────────────────────────
    print(f"\n  fixed={n_fixed}  failed={len(failed_to_fix)}  "
          f"unfixable={len(unfixable)}  post={post_overall}  streak={streak}")
    if result['stable']:
        print(f"  ✅ stack stable — {streak} consecutive clean mornings")

    # exit 0 only if nothing needs a human
    return 0 if (post_overall == 'PASS' and not needs_human) else 1


if __name__ == '__main__':
    sys.exit(main())
