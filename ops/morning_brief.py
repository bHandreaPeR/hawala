"""ops/morning_brief.py — Daily pre-trade signal brief.

Answers the only question that matters each morning, per the trading
checklist GATE 0: **did the system fire, or is today a no-trade day?**

Reads (no live calls — just today's logs/journals the daemons already write):
  - v3 runner logs: did NIFTY / BANKNIFTY print a [PAPER] ENTER today?
  - vol-gate state: are the runners gated (low-vol regime)?
  - VP-Trail: any new entry today?
  - regime: today's range so far vs the compression threshold

Emits a single MACRO-bot Telegram with a blunt verdict:
    🟢 SIGNAL — <what fired> → run the checklist
    ⚪ NO SIGNAL — nothing fired → no trade, close the terminal

The whole point: replace "stare at charts looking for something to do" with
"read one message, mostly it says do nothing, and that's correct."

Run:   python -m ops.morning_brief
Cron:  ~09:20 IST weekdays (after signals would have fired) via launchd.
"""
from __future__ import annotations

import pathlib
import re
import sys
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG_DIR = ROOT / 'logs' / 'trade_bot'


def _today_lines(path: pathlib.Path, day: str) -> list[str]:
    if not path.exists():
        return []
    out = []
    try:
        for ln in path.read_text(errors='ignore').splitlines():
            if ln.startswith(day):
                out.append(ln)
    except Exception:
        pass
    return out


def _runner_status(inst: str, day: str) -> dict:
    """Did this runner ENTER today, get vol-gated, or just sit flat?"""
    log = LOG_DIR / f'runner_{inst.lower()}.log'
    lines = _today_lines(log, day)
    entered = [l for l in lines if '[PAPER] ENTER' in l]
    gated   = [l for l in lines if 'VOL GATE' in l or 'vol_gate' in l and 'BLOCK' in l]
    if entered:
        return {'inst': inst, 'state': 'ENTERED', 'detail': entered[-1][-90:]}
    if gated:
        return {'inst': inst, 'state': 'VOL-GATED', 'detail': 'low-vol regime, blocked'}
    if lines:
        return {'inst': inst, 'state': 'FLAT', 'detail': 'ran, no entry'}
    return {'inst': inst, 'state': 'NO-LOG', 'detail': 'no log lines today'}


def _vptrail_status(day: str) -> dict:
    """Any VP-Trail entry today? Reads the live-daemon log for new_entries."""
    log = LOG_DIR / 'vp_live_daemon.log'
    lines = _today_lines(log, day)
    new = [l for l in lines if 'new_entries=' in l and not l.strip().endswith('new_entries=0')]
    if new:
        return {'state': 'ENTERED', 'detail': new[-1][-90:]}
    if lines:
        return {'state': 'FLAT', 'detail': 'ran, no new entries'}
    return {'state': 'NO-LOG', 'detail': 'no log lines today'}


def _send_telegram(msg: str) -> None:
    env = {}
    try:
        for ln in open(ROOT / 'token.env'):
            if '=' in ln:
                k, _, v = ln.strip().partition('='); env[k] = v
    except Exception:
        print('no token.env'); return
    tok = env.get('TELEGRAM_BOT_TOKEN_MACRO', '').strip()
    chats = [c.strip() for c in env.get('TELEGRAM_CHAT_IDS_MACRO', '').split(',') if c.strip()]
    if not tok or not chats:
        print('telegram disabled'); return
    try:
        from alerts.telegram import send as tg
        for cid in chats:
            tg(tok, cid, msg)
    except Exception as e:
        print(f'telegram err: {e}')


def main() -> None:
    # Calendar gate — silent on non-trading days.
    from ops.market_calendar import is_trading_day, holiday_reason
    today = date.today()
    if not is_trading_day(today):
        msg = (f'📅 <b>{today} — market closed</b> ({holiday_reason(today)}).\n'
               f'No session. No trade. Enjoy the day.')
        print(msg); _send_telegram(msg); return

    day = today.isoformat()
    n  = _runner_status('NIFTY', day)
    bn = _runner_status('BANKNIFTY', day)
    vp = _vptrail_status(day)

    fired = [x for x in (n, bn) if x['state'] == 'ENTERED'] \
          + ([{'inst': 'VP-Trail', **vp}] if vp['state'] == 'ENTERED' else [])

    lines = [f'<b>Hawala morning brief — {day}</b>', '']
    if fired:
        lines.append('🟢 <b>SIGNAL FIRED — run the checklist before acting</b>')
        for f in fired:
            lines.append(f"  • {f.get('inst','?')}: {f.get('detail','')}")
        lines += ['', '⚠️ A signal is NOT a green light. Open the viewer (Clean '
                  'mode), run GATES 1-4 in docs/TRADING_CHECKLIST.md. 1 lot, '
                  'limit order, defined stop. Validation can only veto.']
    else:
        lines.append('⚪ <b>NO SIGNAL — no trade today. Close the terminal.</b>')
        lines += ['',
                  f"  NIFTY runner    : {n['state']} ({n['detail'][:48]})",
                  f"  BANKNIFTY runner: {bn['state']} ({bn['detail'][:48]})",
                  f"  VP-Trail        : {vp['state']} ({vp['detail'][:48]})",
                  '',
                  'The system being quiet IS the instruction: sit out. '
                  'Most days are no-trade days. That is success, not boredom.']
    msg = '\n'.join(lines)
    print(msg)
    _send_telegram(msg)


if __name__ == '__main__':
    main()
