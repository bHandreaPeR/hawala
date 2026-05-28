"""alerts/spot_vix_recorder.py — 1-minute snapshots of spot indices + VIX.

Captures the underlying *spot* values (not the futures) for NIFTY,
BANKNIFTY, SENSEX, plus INDIAVIX (volatility index), via Groww REST
`get_quote()` every POLL_SEC seconds during market hours.

Why this is separate from `tick_recorder.py`:
  - tick_recorder captures FUTURES ticks + depth (high frequency, WS)
  - This captures SPOT (low frequency, REST poll) — spot only updates
    on underlying index ticks, no need for sub-second precision
  - The futures-spot BASIS is a real signal (mean-reverts; blowouts
    predict reversals). We've never captured spot, so we couldn't
    compute basis. Now we can.

Output files (one row per poll per instrument):
  v3/cache/spot_NIFTY_<YYYYMMDD>.csv      schema: ts_ms,ltp,change,change_pct
  v3/cache/spot_BANKNIFTY_<YYYYMMDD>.csv
  v3/cache/spot_SENSEX_<YYYYMMDD>.csv
  v3/cache/vix_<YYYYMMDD>.csv

At 60-s cadence × 6.5h × 4 instruments = ~1560 rows/day total → < 100 KB/day.
Negligible storage; high analytical value.

Run: python -m alerts.spot_vix_recorder
Cron: 11 9 * * 1-5 nohup ... python -m alerts.spot_vix_recorder &
"""
from __future__ import annotations

import csv
import logging
import os
import pathlib
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime

import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / 'v3' / 'cache'
LOG_DIR   = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

POLL_SEC      = float(os.environ.get('SPOT_VIX_POLL_SEC',     '60'))
END_HHMM      = int  (os.environ.get('SPOT_VIX_END_HHMM',   '1535'))
HEARTBEAT_SEC = float(os.environ.get('SPOT_VIX_HEARTBEAT', '300'))

# (display_inst, exchange, segment, trading_symbol)
INSTRUMENTS = [
    ('NIFTY',     'NSE', 'CASH', 'NIFTY'),
    ('BANKNIFTY', 'NSE', 'CASH', 'BANKNIFTY'),
    ('SENSEX',    'BSE', 'CASH', 'SENSEX'),
    ('VIX',       'NSE', 'CASH', 'INDIAVIX'),
]


# ─── Logging ─────────────────────────────────────────────────────────────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record); self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('spot_vix_recorder')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = _FlushingFileHandler(LOG_DIR / 'spot_vix_recorder.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Auth ────────────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    last_err = None
    for attempt in range(3):
        try:
            env = {}
            for ln in open(ROOT / 'token.env'):
                if '=' in ln:
                    k, _, v = ln.strip().partition('=')
                    env[k] = v
            totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            tok  = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(token=tok)
        except Exception as e:
            last_err = e
            log.warning('auth attempt %d failed: %s — retry 5s', attempt + 1, e)
            time.sleep(5)
    raise RuntimeError(f'Groww auth failed: {last_err}')


# ─── CSV writer (lazy per-inst per-day, header-aware) ────────────────────────
class CSVWriter:
    COLS = ['ts_ms', 'ltp', 'change', 'change_pct', 'open', 'high', 'low']

    def __init__(self, path: pathlib.Path):
        self.path = path
        new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, 'a', newline='', buffering=1)
        self._w  = csv.DictWriter(self._fh, fieldnames=self.COLS,
                                  extrasaction='ignore')
        if new:
            self._w.writeheader(); self._fh.flush()

    def write(self, row: dict) -> None:
        self._w.writerow(row); self._fh.flush()

    def close(self) -> None:
        try: self._fh.close()
        except Exception: pass


# ─── Market hours ────────────────────────────────────────────────────────────
def _market_open_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    eh, em = END_HHMM // 100, END_HHMM % 100
    return dtime(9, 15) <= now.time() <= dtime(eh, em)


_running = True


def _signal_handler(signum, frame):
    global _running
    log.info('signal %s — shutting down', signum)
    _running = False


# ─── Main loop ───────────────────────────────────────────────────────────────
def main() -> None:
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info('boot — poll=%.0fs end=%d insts=%s',
             POLL_SEC, END_HHMM, [i[0] for i in INSTRUMENTS])

    # Non-trading day gate — exit cleanly on weekends/holidays
    from ops.market_calendar import is_trading_day, holiday_reason
    if not is_trading_day():
        log.info('non-trading day (%s) — exiting', holiday_reason())
        return

    # Pre-market wait — same robustness pattern as tick_recorder
    while not _market_open_now():
        now = datetime.now()
        eh, em = END_HHMM // 100, END_HHMM % 100
        if now.weekday() >= 5 or now.time() >= dtime(eh, em):
            log.info('market closed for the day (now=%s) — exiting',
                     now.strftime('%H:%M'))
            return
        log.info('pre-market (now=%s) — sleeping 30s', now.strftime('%H:%M'))
        time.sleep(30)

    g = _get_groww()
    log.info('Groww auth OK')

    today = date.today().strftime('%Y%m%d')
    writers: dict = {}
    for inst, _exch, _seg, _ts in INSTRUMENTS:
        fname = (f'vix_{today}.csv' if inst == 'VIX'
                 else f'spot_{inst}_{today}.csv')
        writers[inst] = CSVWriter(CACHE_DIR / fname)

    last_hb     = 0.0
    polls       = 0
    consec_fail = 0

    while _running and _market_open_now():
        t0 = time.time()
        polls += 1
        any_err = False

        for inst, exch, seg, ts_sym in INSTRUMENTS:
            try:
                r = g.get_quote(exchange=exch, segment=seg, trading_symbol=ts_sym)
                ltp     = float(r.get('last_price') or 0.0)
                if ltp == 0:
                    raise ValueError('zero ltp')
                day_open  = float(r.get('day_open')  or 0.0)
                day_high  = float(r.get('day_high')  or 0.0)
                day_low   = float(r.get('day_low')   or 0.0)
                day_close = float(r.get('previous_close') or 0.0)
                change      = ltp - day_close if day_close else 0.0
                change_pct  = (change / day_close * 100.0) if day_close else 0.0
                writers[inst].write({
                    'ts_ms':       int(time.time() * 1000),
                    'ltp':         ltp,
                    'change':      round(change, 2),
                    'change_pct':  round(change_pct, 4),
                    'open':        day_open,
                    'high':        day_high,
                    'low':         day_low,
                })
            except Exception as e:
                any_err = True
                log.warning('%s quote err: %s', inst, e)

        # Auth retry on persistent failures
        if any_err:
            consec_fail += 1
            if consec_fail >= 5:
                log.warning('5 consec failures — re-auth Groww')
                try:
                    g = _get_groww(); consec_fail = 0
                except Exception as e:
                    log.error('re-auth failed: %s — sleep 30s', e)
                    time.sleep(30)
        else:
            consec_fail = 0

        # Heartbeat
        if time.time() - last_hb >= HEARTBEAT_SEC:
            log.info('heartbeat — polls=%d errs=%d', polls, consec_fail)
            last_hb = time.time()

        # Pace
        dt = time.time() - t0
        if dt < POLL_SEC:
            time.sleep(POLL_SEC - dt)

    log.info('EOD — polls=%d', polls)
    for w in writers.values():
        w.close()


if __name__ == '__main__':
    main()
