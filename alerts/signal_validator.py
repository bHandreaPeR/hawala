"""alerts/signal_validator.py — Per-signal checklist validation alert.

When a v3 runner (or VP-Trail) prints a new entry, this daemon runs the
AUTOMATABLE mandatory checks from docs/TRADING_CHECKLIST.md and fires a
SECOND Telegram showing how many passed + the breakdown.

CRITICAL framing: this is a VETO layer, NOT a green light. A full pass does
NOT mean "enter" — the human still owns GATE 2 (the 4 numbers) + GATE 4
(sizing / limit order). A failed check means SKIP. The alert says so.

Auto-checks (objective, computable):
  1. expiry-day trap   — is the option likely expiring today? (theta) [approx]
  2. no thesis-flip    — no opposite-direction system signal in last 30 min
  3. level block       — fat HVN / value-edge / pivot in the trade's path
  4. positioning       — composite not actively contradicting the direction
  5. flow against      — recent footprint delta not strongly opposing

Decoupled: tails the runner / vp_live logs (cursor-based) and queries the
already-running viewer's REST endpoints (/volume_profile, /pivots,
/positioning, /snapshot) for the context. No Groww calls, no runner edits.

Run:   python -m alerts.signal_validator
Cron:  09:12 IST weekdays (alongside the runners) via launchd / cron.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import signal
import sys
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG_DIR   = ROOT / 'logs' / 'trade_bot'
CACHE_DIR = ROOT / 'v3' / 'cache'
STATE     = CACHE_DIR / 'signal_validator_state.json'
VIEWER    = os.environ.get('VALIDATOR_VIEWER', 'http://127.0.0.1:8765')

POLL_SEC   = float(os.environ.get('SIGVAL_POLL_SEC', '15'))
END_HHMM   = int  (os.environ.get('SIGVAL_END_HHMM', '1535'))
# Weekly-expiry weekday per inst (approx; NSE indices = Thursday=3).
EXPIRY_WD  = {'NIFTY': 3, 'BANKNIFTY': 3, 'SENSEX': 1}   # SENSEX Tue=1 (BSE)

# Log sources → (path, inst, is_vptrail)
SOURCES = [
    (LOG_DIR / 'runner_nifty.log',     'NIFTY',     False),
    (LOG_DIR / 'runner_banknifty.log', 'BANKNIFTY', False),
]

RE_ENTRY = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[PAPER\] ENTER (?:BN )?(CE|PE) BUY\s+'
    r'strike=(\d+) @ ([\d.]+).*score=([-\d.]+)')


# ─── Logging ─────────────────────────────────────────────────────────────────
class _FH(logging.FileHandler):
    def emit(self, r): super().emit(r); self.flush()

def _log():
    lg = logging.getLogger('signal_validator'); lg.setLevel(logging.INFO); lg.handlers.clear()
    f = _FH(LOG_DIR / 'signal_validator.log', mode='a')
    f.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s')); lg.addHandler(f)
    s = logging.StreamHandler(); s.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s')); lg.addHandler(s)
    return lg
log = _log()


# ─── Telegram (TRADE bot — same channel as the signals) ──────────────────────
def _tg(msg: str):
    env = {}
    try:
        for ln in open(ROOT / 'token.env'):
            if '=' in ln:
                k, _, v = ln.strip().partition('='); env[k] = v
    except Exception:
        return
    tok = (env.get('TELEGRAM_BOT_TOKEN_TRADE') or env.get('TELEGRAM_BOT_TOKEN_MACRO') or '').strip()
    chats = [c.strip() for c in (env.get('TELEGRAM_CHAT_IDS_TRADE')
             or env.get('TELEGRAM_CHAT_IDS_MACRO') or '').split(',') if c.strip()]
    if not tok or not chats:
        log.warning('no telegram creds'); return
    try:
        from alerts.telegram import send as tg
        for cid in chats: tg(tok, cid, msg)
    except Exception as e:
        log.warning('tg err: %s', e)


# ─── Viewer REST helpers ─────────────────────────────────────────────────────
def _get(path: str):
    try:
        with urllib.request.urlopen(f'{VIEWER}{path}', timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning('viewer %s err: %s', path, e)
        return None


# ─── State (byte cursor per source) ──────────────────────────────────────────
def _load_state() -> dict:
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except Exception: pass
    return {}

def _save_state(s: dict):
    try: STATE.write_text(json.dumps(s))
    except Exception as e: log.warning('state save: %s', e)


# ─── The checks ──────────────────────────────────────────────────────────────
def _check_expiry(inst: str, today: date) -> tuple[bool, str]:
    wd = EXPIRY_WD.get(inst, 3)
    if today.weekday() == wd:
        return False, 'option likely EXPIRES TODAY — theta trap (approx weekly expiry)'
    return True, 'not an expiry-day option (approx)'

def _check_thesis_flip(path: pathlib.Path, inst: str, side: str, sig_dt: datetime) -> tuple[bool, str]:
    # opposite side entry in the last 30 min?
    opp = 'PE' if side == 'CE' else 'CE'
    cutoff = sig_dt - timedelta(minutes=30)
    if not path.exists():
        return True, 'no log to check'
    for ln in path.read_text(errors='ignore').splitlines():
        m = RE_ENTRY.search(ln)
        if not m: continue
        dt = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        if cutoff <= dt < sig_dt and m.group(2) == opp:
            return False, f'OPPOSITE signal ({opp}) fired {dt.strftime("%H:%M")} — thesis-flip risk'
    return True, 'no opposite signal in last 30 min'

def _current_price(inst: str) -> float | None:
    snap = _get(f'/snapshot?inst={inst}&tf=5min')
    if snap and snap.get('candles'):
        return float(snap['candles'][-1]['close'])
    return None

def _check_level_block(inst: str, side: str, cell: float) -> tuple[bool, str]:
    px = _current_price(inst)
    if px is None:
        return True, 'no price to check level block'
    vp  = _get(f'/volume_profile?inst={inst}&scope=prior_day&cell={cell}') or {}
    piv = (_get(f'/pivots?inst={inst}') or {}).get('pivots', {}) or {}
    # block threshold: 0.25% of price OR 3 cells, whichever larger
    thr = max(px * 0.0025, cell * 3)
    levels = []
    for k in ('vah','val','poc'):
        if vp.get(k) is not None: levels.append((k.upper(), vp[k]))
    for h in (vp.get('hvn') or []): levels.append(('HVN', h))
    for k, v in piv.items(): levels.append((k, v))
    if side == 'CE':   # long → resistance above blocks
        ahead = [(n, l) for n, l in levels if l > px and (l - px) <= thr]
        if ahead:
            n, l = min(ahead, key=lambda x: x[1] - px)
            return False, f'LEVEL BLOCK: {n} {l:.0f} is {l-px:.0f} pts above (resistance in path)'
    else:              # short → support below blocks
        ahead = [(n, l) for n, l in levels if l < px and (px - l) <= thr]
        if ahead:
            n, l = min(ahead, key=lambda x: px - x[1])
            return False, f'LEVEL BLOCK: {n} {l:.0f} is {px-l:.0f} pts below (support in path)'
    return True, 'no significant level blocking the path'

def _check_positioning(inst: str, side: str) -> tuple[bool, str]:
    pos = _get(f'/positioning?inst={inst}')
    if not pos:
        return True, 'positioning unavailable'
    comp = pos.get('composite', {})
    d = comp.get('dir', 0); v = comp.get('value', 0)
    want = 1 if side == 'CE' else -1
    if d != 0 and d != want:
        return False, f'POSITIONING CONTRADICTS: composite {v:+.2f} ({comp.get("label")}) vs trade'
    return True, f'positioning not contradicting (composite {v:+.2f})'

def _check_flow(inst: str, side: str) -> tuple[bool, str]:
    snap = _get(f'/snapshot?inst={inst}&tf=5min')
    if not snap or not snap.get('candles'):
        return True, 'no flow data'
    last3 = snap['candles'][-3:]
    dsum = sum(c.get('delta_qty', 0) for c in last3)
    want = 1 if side == 'CE' else -1
    if want > 0 and dsum < 0:
        return False, f'FLOW AGAINST: last-3-bar delta {dsum:+.0f} (sellers) vs long'
    if want < 0 and dsum > 0:
        return False, f'FLOW AGAINST: last-3-bar delta {dsum:+.0f} (buyers) vs short'
    return True, f'recent flow not opposing (Δ {dsum:+.0f})'


# ─── Validate one signal ─────────────────────────────────────────────────────
def validate(path: pathlib.Path, inst: str, m: re.Match):
    sig_dt = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    side   = m.group(2)          # CE / PE
    strike = m.group(3)
    price  = m.group(4)
    score  = m.group(5)
    dir_lbl = 'LONG' if side == 'CE' else 'SHORT'
    cell = 5.0 if inst == 'NIFTY' else 10.0
    today = date.today()

    checks = [
        _check_expiry(inst, today),
        _check_thesis_flip(path, inst, side, sig_dt),
        _check_level_block(inst, side, cell),
        _check_positioning(inst, side),
        _check_flow(inst, side),
    ]
    n_pass = sum(1 for ok, _ in checks if ok)
    n_tot  = len(checks)

    head = (f'🔎 <b>CHECKLIST VALIDATION — {inst} {side} {strike} {dir_lbl}</b>\n'
            f'signal {sig_dt.strftime("%H:%M:%S")} @ {price}  score={score}\n\n'
            f'<b>Mandatory auto-checks: {n_pass}/{n_tot} pass</b>')
    body = []
    for ok, detail in checks:
        body.append(f'{"✅" if ok else "❌ VETO —"} {detail}')
    foot = ('\n⚠️ <b>NOT a green light.</b> Still YOURS: GATE 2 (write the 4 '
            'numbers), GATE 4 (1 lot, limit order, ≤5% risk). '
            'Any ❌ VETO → <b>SKIP</b>.')
    if n_pass < n_tot:
        foot = ('\n🛑 <b>One or more VETOES fired — default action is SKIP.</b> '
                'Only override with a written reason.') + foot
    msg = head + '\n' + '\n'.join(body) + '\n' + foot
    log.info('validated %s %s %s → %d/%d pass', inst, side, strike, n_pass, n_tot)
    _tg(msg)


# ─── Market hours ────────────────────────────────────────────────────────────
def _open_now() -> bool:
    from ops.market_calendar import is_trading_day
    now = datetime.now()
    if not is_trading_day(now.date()):
        return False
    eh, em = END_HHMM // 100, END_HHMM % 100
    return dtime(9, 12) <= now.time() <= dtime(eh, em)


_running = True
def _sig(s, f):
    global _running; log.info('signal %s — stop', s); _running = False


def main():
    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    from ops.market_calendar import is_trading_day, holiday_reason
    if not is_trading_day():
        log.info('non-trading day (%s) — exiting', holiday_reason()); return

    # Pre-market wait
    while _running and not _open_now():
        now = datetime.now()
        if now.time() >= dtime(END_HHMM // 100, END_HHMM % 100):
            log.info('past EOD — exit'); return
        log.info('pre-market (%s) — sleep 30s', now.strftime('%H:%M')); time.sleep(30)

    log.info('boot — poll=%.0fs viewer=%s', POLL_SEC, VIEWER)
    state = _load_state()
    # Seed cursors to current EOF so we don't re-alert the whole day's history.
    for path, inst, _ in SOURCES:
        key = path.name
        if key not in state and path.exists():
            state[key] = path.stat().st_size
    _save_state(state)

    while _running and _open_now():
        for path, inst, _ in SOURCES:
            if not path.exists(): continue
            key = path.name
            size = path.stat().st_size
            last = state.get(key, size)
            if size <= last:
                state[key] = size; continue
            with path.open('rb') as f:
                f.seek(last); chunk = f.read().decode('utf-8', errors='ignore')
            state[key] = size
            for ln in chunk.splitlines():
                m = RE_ENTRY.search(ln)
                if m:
                    try: validate(path, inst, m)
                    except Exception as e: log.exception('validate err: %s', e)
        _save_state(state)
        time.sleep(POLL_SEC)

    log.info('EOD — exit')


if __name__ == '__main__':
    main()
