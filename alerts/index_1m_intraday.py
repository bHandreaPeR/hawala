"""alerts/index_1m_intraday.py — Intraday 1m cache freshness daemon.

Independent fetcher that keeps the per-instrument 1m caches fresh DURING
market hours, decoupled from the v3 runners.

Why this exists
---------------
The VP-Trail live daemon polls `v3/cache/candles_1m_<INST>.pkl` to detect
new entries. That cache is written intraday ONLY by the v3 runner for
that instrument. If a runner exits early (regime/vol gate, crash),
the cache freezes for the rest of the day and the VP daemon stops
seeing fresh bars → silently misses entries.

Forensics on 2026-05-20: BN runner exited at 09:12 (regime gate, 5d return
−0.60% < 1.0%). VP daemon polled BN ~70 times that day, every poll saw
`last bar = 2026-05-19 15:30:00`. The Wed 11:15 BN +₹10,677 VP entry got
skipped because the daemon couldn't see Wed's bars.

This daemon fixes that. Every POLL_SEC (default 60s) it pulls the latest
1m candles for NIFTY / BANKNIFTY / SENSEX front-month futures from Groww
REST and merges into the cache. The cache is then ALWAYS fresh, regardless
of v3 runner state.

Run:    python -m alerts.index_1m_intraday
Cron:   11 9 * * 1-5  cd ... && nohup ... -m alerts.index_1m_intraday \\
            > /dev/null 2>&1 &
"""
from __future__ import annotations

import os
import sys
import time
import signal
import pickle
import logging
import pathlib
import threading
from datetime import datetime, date, timedelta, time as dtime

import pandas as pd
import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Re-use existing per-instrument fetcher logic to keep contract resolution +
# column normalisation identical to the EOD batch (so the live writes look
# byte-for-byte like daily_fetch.sh outputs).
from v3.data.fetch_1m_NIFTY     import _fetch_day_1m as fetch_nifty  # noqa: E402
from v3.data.fetch_1m_BANKNIFTY import _fetch_day_1m as fetch_bn     # noqa: E402
from v3.data.fetch_1m_SENSEX    import _fetch_day_1m as fetch_sx     # noqa: E402

CACHE_DIR = ROOT / 'v3' / 'cache'
LOG_DIR   = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

POLL_SEC   = float(os.environ.get('INDEX_1M_POLL_SEC',   '60'))
END_HHMM   = int  (os.environ.get('INDEX_1M_END_HHMM',  '1535'))
HEARTBEAT  = float(os.environ.get('INDEX_1M_HEARTBEAT', '300'))   # 5 min
INSTRUMENTS = [s.strip() for s in
               os.environ.get('INDEX_1M_INSTRUMENTS',
                              'NIFTY,BANKNIFTY,SENSEX').split(',') if s.strip()]

FETCHERS = {'NIFTY': fetch_nifty, 'BANKNIFTY': fetch_bn, 'SENSEX': fetch_sx}
CACHE_PATH = {i: CACHE_DIR / f'candles_1m_{i}.pkl' for i in FETCHERS}

_running = True


# ─── Logging ─────────────────────────────────────────────────────────────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record); self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('index_1m_intraday')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = _FlushingFileHandler(LOG_DIR / 'index_1m_intraday.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Groww auth (re-used pattern w/ retry) ────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    last = None
    for attempt in range(3):
        try:
            env = {}
            with open(ROOT / 'token.env') as f:
                for ln in f:
                    if '=' in ln:
                        k, _, v = ln.strip().partition('=')
                        env[k] = v
            totp  = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(token=token)
        except Exception as e:
            last = e
            log.warning('Groww auth attempt %d failed: %s — retrying 5s',
                        attempt + 1, e)
            time.sleep(5)
    raise RuntimeError(f'Groww auth failed after 3 attempts: {last}')


# ─── Cache merge helper ──────────────────────────────────────────────────────
_lock = threading.Lock()


def _merge_into_cache(inst: str, new_df: pd.DataFrame) -> tuple[int, str]:
    """Merge new bars into the v3 1m cache. Returns (rows_added, last_ts_str)."""
    if new_df.empty:
        return 0, '∅'

    path = CACHE_PATH[inst]
    with _lock:
        if path.exists():
            with open(path, 'rb') as f:
                old = pickle.load(f)
        else:
            old = pd.DataFrame()

        combined = pd.concat([old, new_df], ignore_index=True)
        combined['ts'] = pd.to_datetime(combined['ts'])
        before = len(combined)
        combined = combined.drop_duplicates(subset=['ts'], keep='last')
        combined = combined.sort_values('ts').reset_index(drop=True)
        added = len(combined) - len(old) if not old.empty else len(combined)

        # Atomic write — write tmp then rename
        tmp = path.with_suffix('.pkl.tmp')
        with open(tmp, 'wb') as f:
            pickle.dump(combined, f)
        tmp.replace(path)

        return added, str(combined['ts'].max())


# ─── Market-hour check ───────────────────────────────────────────────────────
def _market_open_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    eh, em = END_HHMM // 100, END_HHMM % 100
    return dtime(9, 15) <= now.time() <= dtime(eh, em)


def _signal_handler(signum, frame):
    global _running
    log.info('signal %s — shutting down', signum)
    _running = False


# ─── Main poll loop ──────────────────────────────────────────────────────────
def main() -> None:
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info('boot — instruments=%s poll=%.0fs end=%d', INSTRUMENTS, POLL_SEC, END_HHMM)

    # Non-trading day gate — weekends/holidays exit cleanly
    from ops.market_calendar import is_trading_day, holiday_reason
    if not is_trading_day():
        log.info('non-trading day (%s) — exiting', holiday_reason())
        return

    # If cron fired us slightly before 09:15 (e.g. 09:11), wait — don't exit.
    # Otherwise we'd miss the morning entirely until the next cron tick.
    while not _market_open_now():
        now = datetime.now()
        if now.weekday() >= 5 or now.time() >= dtime(*divmod(END_HHMM, 100)):
            log.info('market closed for the day (now=%s) — exiting', now.strftime('%H:%M'))
            return
        log.info('pre-market (now=%s) — sleeping 30s', now.strftime('%H:%M'))
        time.sleep(30)

    g = _get_groww()
    log.info('Groww auth OK')

    today = date.today()
    last_hb = 0.0
    poll_count = 0
    error_streak = 0

    while _running and _market_open_now():
        t0 = time.time()
        poll_count += 1

        for inst in INSTRUMENTS:
            if inst not in FETCHERS:
                log.warning('unknown inst %s — skip', inst); continue
            try:
                # Fetch the FULL day's 1m bars. _fetch_day_1m handles slicing
                # 09:15-15:30 internally. We deduplicate against existing cache
                # so re-fetching the same day is cheap + idempotent.
                df = FETCHERS[inst](g, today)
                added, last = _merge_into_cache(inst, df)
                if added > 0:
                    log.info('%s: +%d bars merged (last=%s)', inst, added, last)
                error_streak = 0
            except Exception as e:
                error_streak += 1
                log.warning('%s fetch err (streak=%d): %s', inst, error_streak, e)
                # Re-auth on persistent failure
                if error_streak >= 5:
                    log.warning('5 consecutive errors — re-authing Groww')
                    try:
                        g = _get_groww()
                        error_streak = 0
                    except Exception as e2:
                        log.error('re-auth failed: %s — sleep 30s', e2)
                        time.sleep(30)

        # Heartbeat
        if time.time() - last_hb >= HEARTBEAT:
            log.info('heartbeat — polls=%d', poll_count)
            last_hb = time.time()

        # Pace: maintain POLL_SEC spacing
        dt = time.time() - t0
        if dt < POLL_SEC:
            time.sleep(POLL_SEC - dt)

    log.info('EOD — polls=%d', poll_count)


if __name__ == '__main__':
    main()
