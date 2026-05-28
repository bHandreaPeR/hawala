"""alerts/vp_paper_executor.py — Intraday VP-Trail exit watcher.

Why this exists
---------------
`vp_paper_journal.py` runs once a day at 16:35 IST after the EOD cache
refresh. That means if a VP-Trail trade hits a TRAIL STOP / BREAKEVEN /
EARLY CUT / TARGET HIT at 11:30, you don't see the alert until 16:35 —
five hours late. For paper-trading purposes that's tolerable, but if you
ever execute these for real money you want to know NOW.

This daemon polls the VP backtest every POLL_SEC during market hours.
Whenever the backtest reveals a newly-closed trade (cursor in
.vp_paper_state.json hasn't seen this exit_ts yet), it:
  1. Appends the row to `trade_logs/vp_paper_journal.csv` (same file as the
     16:35 batch — single source of truth).
  2. Sends a TRADE-bot Telegram with entry → exit → reason → P&L.

The 16:35 EOD batch still runs and is harmless — it will find no NEW
trades to journal because the executor already caught them all during
the day. It still fires the daily summary Telegram.

Reuses 100% of vp_paper_journal.collect_new_closed_trades() — no new
exit-rule code. The backtest IS the exit logic.

Cron:  12 9 * * 1-5  cd ... && nohup ... -m alerts.vp_paper_executor &
"""
from __future__ import annotations

import os
import sys
import time
import signal
import logging
import pathlib
from datetime import datetime, time as dtime

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alerts.telegram import send as tg_send                            # noqa: E402
from alerts.vp_paper_journal import (                                  # noqa: E402
    collect_new_closed_trades,
    _append_journal,
    _load_creds,
    LOT_SIZE,
)

LOG_DIR  = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

POLL_SEC   = float(os.environ.get('VP_EXEC_POLL_SEC',   '60'))
END_HHMM   = int  (os.environ.get('VP_EXEC_END_HHMM',  '1535'))
HEARTBEAT  = float(os.environ.get('VP_EXEC_HEARTBEAT', '300'))   # 5 min


# ─── Logging (flushing handler — heartbeats reach disk) ──────────────────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record); self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('vp_paper_executor')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = _FlushingFileHandler(LOG_DIR / 'vp_paper_executor.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Telegram message for a single closed trade ──────────────────────────────
def _format_close(row: dict) -> str:
    tag   = '🟢' if row['pnl_rs'] >= 0 else '🔴'
    sign  = '+' if row['pnl_rs'] >= 0 else ''
    side  = row['direction']
    inst  = row['inst']
    pnl_p = row['pnl_pts']
    return (
        f'{tag} <b>VP-TRAIL CLOSED — {inst} {side}</b>\n'
        f'<b>{row["entry"]:,.1f} → {row["exit"]:,.1f}</b>  ({sign}{pnl_p:+.1f} pts)\n'
        f'<b>P&amp;L:</b> ₹{sign}{row["pnl_rs"]:+,.0f}\n'
        f'Reason: <i>{row["exit_reason"]}</i>  '
        f'Held: {row["days_held"]} day(s)  Lot: {row["lot_size"]}\n'
        f'<i>Paper-trade. Mark-to-backtest exits.</i>'
    )


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

    log.info('boot — poll=%.0fs end=%d', POLL_SEC, END_HHMM)

    # Non-trading day gate — weekends/holidays exit cleanly
    from ops.market_calendar import is_trading_day, holiday_reason
    if not is_trading_day():
        log.info('non-trading day (%s) — exiting', holiday_reason())
        return

    # If cron fired us slightly before 09:15 (e.g. 09:12), wait — don't exit.
    while not _market_open_now():
        now = datetime.now()
        eh, em = END_HHMM // 100, END_HHMM % 100
        if now.weekday() >= 5 or now.time() >= dtime(eh, em):
            log.info('market closed for the day (now=%s) — exiting',
                     now.strftime('%H:%M'))
            return
        log.info('pre-market (now=%s) — sleeping 30s', now.strftime('%H:%M'))
        time.sleep(30)

    token, chat_ids = _load_creds()
    if not token or not chat_ids:
        log.warning('No telegram creds — will journal silently')

    last_hb = 0.0
    polls = 0
    total_alerted = 0

    while _running and _market_open_now():
        t0 = time.time()
        polls += 1

        try:
            new_trades = collect_new_closed_trades()
        except Exception as e:
            log.exception('backtest error: %s', e)
            new_trades = pd.DataFrame()

        if not new_trades.empty:
            _append_journal(new_trades)
            log.info('+%d new closed trade(s) at poll #%d',
                     len(new_trades), polls)
            if token and chat_ids:
                for _, r in new_trades.iterrows():
                    msg = _format_close(r.to_dict())
                    for cid in chat_ids:
                        try:
                            tg_send(token, cid, msg)
                        except Exception as e:
                            log.warning('telegram send failed: %s', e)
                    total_alerted += 1

        # Heartbeat
        if time.time() - last_hb >= HEARTBEAT:
            log.info('heartbeat — polls=%d alerted=%d', polls, total_alerted)
            last_hb = time.time()

        # Pace
        dt = time.time() - t0
        if dt < POLL_SEC:
            time.sleep(POLL_SEC - dt)

    log.info('EOD — polls=%d alerted=%d', polls, total_alerted)


if __name__ == '__main__':
    main()
