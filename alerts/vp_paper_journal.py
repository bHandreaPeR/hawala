"""alerts/vp_paper_journal.py — Paper-trade journal for VP-Trail-Swing.

Runs once a day after `daily_fetch.sh` refreshes the 1m caches (cron 16:35
IST). Replays `run_vp_trailing_swing` on the latest data for NIFTY,
BANKNIFTY, SENSEX, diffs against the existing journal CSV, appends any
newly-closed trades, and sends a TRADE-bot Telegram summary.

Files:
    trade_logs/vp_paper_journal.csv   — append-only ledger of closed trades
    alerts/.vp_paper_state.json       — last-seen exit_ts per instrument

Why this exists
---------------
`vp_live_daemon.py` only emits ENTRY alerts (one Telegram per alert) and
some entries get skipped when the cache hasn't been refreshed intraday.
This journal is the authoritative SOURCE OF TRUTH for hypothetical
paper-trade P&L — it never misses a trade because it runs after EOD
when the cache is complete.

Cron line:
    35 16 * * 1-5 cd ... && /opt/anaconda3/bin/python3 -m alerts.vp_paper_journal \
        >> logs/trade_bot/vp_paper_journal-$(date +\\%Y\\%m\\%d).log 2>&1
"""
from __future__ import annotations

import json
import logging
import os
import sys
import pathlib
from datetime import datetime, date, timedelta

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alerts.telegram import send as tg_send                               # noqa: E402
from alerts.vp_live_daemon import _load, INSTRUMENTS, CANONICAL_PARAMS    # noqa: E402
from strategies.vp_trailing_swing import run_vp_trailing_swing            # noqa: E402

JOURNAL_CSV = ROOT / 'trade_logs' / 'vp_paper_journal.csv'
STATE_FILE  = ROOT / 'alerts' / '.vp_paper_state.json'
LOG_DIR     = ROOT / 'logs' / 'trade_bot'

LOT_SIZE = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 10}

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('vp_paper_journal')


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_journal() -> pd.DataFrame:
    if JOURNAL_CSV.exists():
        return pd.read_csv(JOURNAL_CSV)
    return pd.DataFrame()


def _append_journal(rows: pd.DataFrame) -> None:
    JOURNAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    header = not JOURNAL_CSV.exists()
    rows.to_csv(JOURNAL_CSV, mode='a', header=header, index=False)


def _load_creds() -> tuple[str | None, list[str]]:
    """Pull TRADE-bot creds — matches alert_runner / runner_nifty pattern."""
    env = {}
    env_path = ROOT / 'token.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k] = v
    token = env.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
    raw   = env.get('TELEGRAM_CHAT_IDS')  or os.environ.get('TELEGRAM_CHAT_IDS', '')
    chat_ids = [c.strip() for c in raw.split(',') if c.strip()]
    return token, chat_ids


# ─── Core: replay + diff ─────────────────────────────────────────────────────
def collect_new_closed_trades() -> pd.DataFrame:
    """Run VP backtest on every instrument; return rows whose exit_ts is
    after the per-inst journal cursor."""
    state = _load_state()
    new_rows: list[dict] = []

    for inst in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
        cfg = INSTRUMENTS.get(inst); sp = CANONICAL_PARAMS.get(inst)
        if cfg is None or sp is None:
            log.warning('%s: no canonical config — skip', inst); continue

        df = _load(inst)
        if df.empty:
            log.warning('%s: empty data — skip', inst); continue

        try:
            full_log = run_vp_trailing_swing(df, cfg, sp)
        except Exception as e:
            log.exception('%s: backtest failed: %s', inst, e); continue
        if full_log.empty:
            log.info('%s: no trades in backtest', inst); continue

        full_log = full_log.copy()
        full_log['inst']     = inst
        full_log['lot_size'] = LOT_SIZE[inst]
        full_log['entry_ts'] = pd.to_datetime(full_log['entry_ts'])
        full_log['exit_ts']  = pd.to_datetime(full_log['exit_ts'])

        cursor = state.get(inst)
        cursor_ts = pd.Timestamp(cursor) if cursor else pd.Timestamp('2000-01-01')
        fresh = full_log[full_log['exit_ts'] > cursor_ts]
        log.info('%s: %d new closed trades since cursor %s',
                 inst, len(fresh), cursor or '∅')

        for _, r in fresh.iterrows():
            new_rows.append({
                'journaled_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'inst':         inst,
                'entry_ts':     str(r['entry_ts']),
                'exit_ts':      str(r['exit_ts']),
                'direction':    r['direction'],
                'entry':        round(float(r['entry']),       2),
                'exit':         round(float(r['exit_price']),  2),
                'stop':         round(float(r['stop']),        2),
                'target':       round(float(r['target']),      2),
                'pnl_pts':      round(float(r['pnl_pts']),     2),
                'pnl_rs':       round(float(r['pnl_rs']),      2),
                'win':          int(r['win']),
                'exit_reason':  r['exit_reason'],
                'days_held':    int(r.get('days_held', 0)),
                'lot_size':     LOT_SIZE[inst],
                'atr14':        round(float(r.get('atr14', 0)), 2),
                'regime':       r.get('regime', ''),
            })

        # Advance cursor past every evaluated exit, even old ones — prevents
        # re-emission if exits get re-derived between runs.
        if not full_log.empty:
            state[inst] = str(full_log['exit_ts'].max())

    _save_state(state)
    return pd.DataFrame(new_rows)


# ─── Telegram summary ────────────────────────────────────────────────────────
def _build_message(new_trades: pd.DataFrame, journal: pd.DataFrame) -> str:
    today = date.today()
    if not new_trades.empty:
        new_trades = new_trades.copy()
        new_trades['exit_d'] = pd.to_datetime(new_trades['exit_ts']).dt.date

    todays = new_trades[new_trades.get('exit_d') == today] if not new_trades.empty else pd.DataFrame()

    # Week-to-date P&L from full journal
    if journal.empty:
        wtd_rs, wtd_n, wtd_wr = 0.0, 0, 0.0
    else:
        journal = journal.copy()
        journal['exit_d'] = pd.to_datetime(journal['exit_ts']).dt.date
        week_start = today - timedelta(days=today.weekday())   # Monday
        wtd = journal[(journal['exit_d'] >= week_start) & (journal['exit_d'] <= today)]
        wtd_rs = wtd['pnl_rs'].sum() if not wtd.empty else 0.0
        wtd_n  = len(wtd)
        wtd_wr = (wtd['win'].mean() * 100) if wtd_n else 0.0

    lines = [f'<b>📓 VP-Trail Paper Journal — {today:%a %d %b %Y}</b>']

    if todays.empty:
        lines.append('\n<i>No VP trades closed today.</i>')
    else:
        lines.append(f'\n<b>Closed today: {len(todays)}</b>')
        for _, r in todays.iterrows():
            tag = '🟢' if r['pnl_rs'] >= 0 else '🔴'
            lines.append(
                f'{tag} <b>{r["inst"]}</b> {r["direction"]}  '
                f'<code>{r["entry"]:,.1f} → {r["exit"]:,.1f}</code>  '
                f'({r["pnl_pts"]:+.1f} pts · ₹{r["pnl_rs"]:+,.0f})  '
                f'<i>{r["exit_reason"]}</i>'
            )
        day_pnl = todays['pnl_rs'].sum()
        lines.append(f'\n<b>Today P&amp;L:</b> ₹{day_pnl:+,.0f}  '
                     f'({(todays["win"].mean()*100):.0f}% WR)')

    lines.append(f'\n<b>Week-to-date</b> ({wtd_n} trades, '
                 f'{wtd_wr:.0f}% WR): <b>₹{wtd_rs:+,.0f}</b>')
    lines.append('\n<i>Paper-trade. Mark-to-backtest exits. Not real fills.</i>')
    return '\n'.join(lines)


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.info('VP paper journal — start')

    new_trades = collect_new_closed_trades()
    if not new_trades.empty:
        _append_journal(new_trades)
        log.info('appended %d trades to %s', len(new_trades), JOURNAL_CSV.name)

    journal = _load_journal()
    msg = _build_message(new_trades, journal)
    log.info('summary built — %d new, journal has %d total',
             len(new_trades), len(journal))

    token, chat_ids = _load_creds()
    if not token or not chat_ids:
        log.warning('No telegram creds — printing message only')
        print(msg.replace('<b>','').replace('</b>','')
                  .replace('<i>','').replace('</i>','')
                  .replace('<code>','').replace('</code>','')
                  .replace('&amp;','&'))
        return
    for cid in chat_ids:
        tg_send(token, cid, msg)
    log.info('telegram sent to %d chat(s)', len(chat_ids))


if __name__ == '__main__':
    main()
