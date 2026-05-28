"""ops/market_calendar.py — NSE/BSE trading-day helper.

Tells the rest of the stack whether `now` is a market trading day. Used by:
  - recorders (tick_recorder, spot_vix_recorder, index_1m_intraday,
    vp_paper_executor) → exit cleanly at boot on a non-trading day instead
    of looping in pre-market wait
  - monitor (ops/monitor.py) → skip market-hours-only health checks on
    holidays, so wedged-looking-but-actually-correct silent daemons don't
    trigger restart/escalation cascades

The holiday list lives in `ops/market_holidays.json` (one file, both
exchanges treated together — NSE and BSE share virtually all holidays
in practice). Edit that JSON freely; this module re-reads it on each
call. No code change needed when a new year's holidays drop.

Why this exists: 2026-05-28 was a market holiday. The recorders launched
on schedule, subscribed to WS, then sat with no LTP messages because NSE
wasn't trading. Watchdog fired, reconnect loop ran, vol_poll errored —
all looking exactly like infrastructure failure. Hours wasted chasing
phantom bugs. This module prevents the noise.

Public API:
    is_trading_day(d: date = today) -> bool
    next_trading_day(from_d: date = today) -> date
    holiday_reason(d: date) -> Optional[str]
    summary() -> str   # human-readable status of TODAY
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, timedelta
from typing import Optional

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HOLIDAYS_PATH = _ROOT / 'ops' / 'market_holidays.json'


def _load_holidays() -> dict[date, str]:
    """Parse the JSON. Tolerates underscore-prefixed metadata keys.
    Returns {date: reason}."""
    out: dict[date, str] = {}
    try:
        with _HOLIDAYS_PATH.open() as f:
            raw = json.load(f)
    except Exception:
        return out
    for k, v in raw.items():
        if k.startswith('_'):
            continue
        try:
            out[date.fromisoformat(k)] = str(v)
        except Exception:
            continue
    return out


def is_trading_day(d: Optional[date] = None) -> bool:
    """True iff `d` (default: today) is a weekday AND not in the holiday list."""
    d = d or date.today()
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return d not in _load_holidays()


def holiday_reason(d: Optional[date] = None) -> Optional[str]:
    """Return the reason `d` is a non-trading day, or None if it's a normal
    trading weekday. Distinguishes 'weekend' from 'holiday: <name>'."""
    d = d or date.today()
    if d.weekday() == 5:
        return 'Saturday'
    if d.weekday() == 6:
        return 'Sunday'
    reason = _load_holidays().get(d)
    if reason:
        return f'holiday: {reason}'
    return None


def next_trading_day(from_d: Optional[date] = None) -> date:
    """Smallest date > from_d that is a trading day. Bounded scan (max
    20 days forward — covers any plausible holiday cluster)."""
    d = (from_d or date.today()) + timedelta(days=1)
    for _ in range(20):
        if is_trading_day(d):
            return d
        d += timedelta(days=1)
    raise RuntimeError(f'no trading day found in 20 days after {from_d}')


def summary() -> str:
    """One-line human-readable status for the boot logs of any recorder."""
    today = date.today()
    if is_trading_day(today):
        return f'today ({today}) is a trading day'
    nxt = next_trading_day(today)
    return (f'today ({today}) is NOT a trading day — {holiday_reason(today)}. '
            f'Next trading day: {nxt} ({nxt.strftime("%A")}).')


if __name__ == '__main__':
    import sys
    print(summary())
    sys.exit(0 if is_trading_day() else 1)
