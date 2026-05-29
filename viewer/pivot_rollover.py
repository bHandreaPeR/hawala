"""viewer/pivot_rollover.py — Monthly futures-rollover pivot correction.

WHY THIS EXISTS
---------------
Floor pivots use the prior trading day's futures OHLC. That OHLC comes from the
live 1m candle cache (candles_1m_<inst>.pkl), which records whatever contract
the tick recorder is subscribed to. The recorder rolls to the new front month
the day AFTER expiry — so on expiry day it is still recording the EXPIRING
contract.

On the first session of a new contract (the "rollover day") the prior trading
day in the cache therefore belongs to the OLD, expiring contract, which trades
~inter-month-basis lower than the new front month (~60 pts for NIFTY). Using it
anchors that day's pivots to the wrong contract — off by the basis.

Verified example (NIFTY, May→Jun 2026):
    live cache 2026-05-26 = 26-May contract   C=23911.3   (expiring)
    30-Jun contract on 2026-05-26             C=23997.2   (new front month)
    → 05-27 pivots were ~60 pts too low.

WHAT THIS DOES
--------------
Detects a rollover (front-month contract for `today` differs from the contract
of the prior trading day) and writes an authoritative override file with the
NEW front-month contract's OHLC for the prior session, fetched from Groww
historical. The viewer's `_prior_day_ohlc` prefers this override when its
`prior_day` matches the computed prior trading day.

On every non-rollover day it does only a cached get_expiries lookup (no
historical-candle fetch, no file written), so it is cheap to run every morning;
the heavier OHLC fetch happens ~1 day/month. autoheal invokes it at 06:55 IST.

Roll convention here = smallest monthly expiry >= date (roll the day AFTER
expiry), matching the live recorder. Do NOT change this without re-checking the
recorder's subscription logic.

CLI:
    python -m viewer.pivot_rollover                # all instruments
    python -m viewer.pivot_rollover --inst NIFTY
    python -m viewer.pivot_rollover --date 2026-05-27   # test a past rollover
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATE_DIR = ROOT / 'viewer' / 'state'

# inst → (exchange, ticker). SENSEX trades on BSE; index futures on NSE.
INSTRUMENT_META = {
    'NIFTY':     ('NSE', 'NIFTY'),
    'BANKNIFTY': ('NSE', 'BANKNIFTY'),
    'SENSEX':    ('BSE', 'SENSEX'),
}


def _override_path(inst: str) -> Path:
    return STATE_DIR / f'pivot_override_{inst}.json'


def _get_groww():
    """Authenticated Groww client (same pattern as v3 fetchers)."""
    import pyotp
    from growwapi import GrowwAPI
    env = {}
    with open(ROOT / 'token.env') as f:
        for line in f:
            if '=' in line and not line.strip().startswith('#'):
                k, _, v = line.strip().partition('=')
                env[k] = v
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
    return GrowwAPI(token=token)


def _prior_trading_day(today: date) -> date:
    """Most recent trading day strictly before `today` (skips weekends +
    NSE/BSE holidays via ops.market_calendar; falls back to weekday-only)."""
    try:
        from ops.market_calendar import is_trading_day
        d = today - timedelta(days=1)
        for _ in range(15):
            if is_trading_day(d):
                return d
            d -= timedelta(days=1)
    except Exception:
        pass
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _front_month_expiry(groww, exchange: str, ticker: str,
                        on_date: date) -> date | None:
    """Active monthly futures expiry for `on_date` under the recorder's roll
    convention: the smallest MONTHLY expiry that is >= on_date (roll the day
    AFTER expiry). Returns None if expiries can't be resolved."""
    from data.contract_resolver import _fetch_expiries_for_month

    # Gather monthly expiries for the current + next two months.
    monthly: dict[tuple[int, int], date] = {}
    y, m = on_date.year, on_date.month
    for _ in range(3):
        for exp in _fetch_expiries_for_month(groww, ticker, y, m,
                                             exchange=exchange):
            key = (exp.year, exp.month)
            if key not in monthly or exp > monthly[key]:
                monthly[key] = exp        # last expiry of month = monthly future
        m += 1
        if m > 12:
            m, y = 1, y + 1

    for exp in sorted(monthly.values()):
        if exp >= on_date:
            return exp
    return None


def _fetch_day_ohlc(groww, symbol: str, exchange: str,
                    d: date) -> dict | None:
    """Fetch one trading day's OHLC for `symbol` from Groww historical."""
    try:
        r = groww.get_historical_candles(
            exchange=exchange, segment='FNO', groww_symbol=symbol,
            start_time=f'{d} 09:15:00', end_time=f'{d} 15:30:00',
            candle_interval=groww.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', []) if isinstance(r, dict) else []
        if not candles:
            return None
        return {
            'open':  float(candles[0][1]),
            'high':  float(max(c[2] for c in candles)),
            'low':   float(min(c[3] for c in candles)),
            'close': float(candles[-1][4]),
        }
    except Exception as e:
        print(f"  ⚠ historical fetch failed {symbol} {d}: {e}")
        return None


def maybe_backfill(inst: str, today: date, groww=None) -> dict | None:
    """Detect a rollover for `inst` on `today`; if found, write the override
    file with the NEW front-month contract's prior-session OHLC and return it.
    Returns None on a non-rollover day (and clears any stale override)."""
    if inst not in INSTRUMENT_META:
        return None
    exchange, ticker = INSTRUMENT_META[inst]
    from data.contract_resolver import build_futures_symbol

    if groww is None:
        groww = _get_groww()

    prior = _prior_trading_day(today)
    fm_today = _front_month_expiry(groww, exchange, ticker, today)
    fm_prior = _front_month_expiry(groww, exchange, ticker, prior)
    if not fm_today or not fm_prior:
        print(f"  [{inst}] expiry resolution failed "
              f"(today={fm_today}, prior={fm_prior}) — skip")
        return None

    if fm_today == fm_prior:
        # No rollover. Drop any stale override so it can't be misapplied.
        p = _override_path(inst)
        if p.exists():
            p.unlink()
        print(f"  [{inst}] no rollover (front month {fm_today} unchanged)")
        return None

    # Rollover: the cache's prior-day row is the OLD contract. Pull the NEW
    # front month's OHLC for that same prior session from Groww historical.
    symbol = build_futures_symbol(exchange, ticker, fm_today)
    ohlc = _fetch_day_ohlc(groww, symbol, exchange, prior)
    if not ohlc:
        print(f"  [{inst}] ROLLOVER {fm_prior}→{fm_today} but no historical "
              f"OHLC for {symbol} on {prior} — leaving cache value")
        return None

    payload = {
        'inst': inst,
        'prior_day': str(prior),
        'contract': symbol,
        'front_month_expiry': str(fm_today),
        'rolled_from_expiry': str(fm_prior),
        'ohlc': ohlc,
        'written_ts': datetime.now().isoformat(),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _override_path(inst).with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_override_path(inst))
    print(f"  [{inst}] ROLLOVER {fm_prior}→{fm_today}: override prior={prior} "
          f"{symbol} OHLC={ohlc}")
    return payload


def load_override(inst: str, prior_day: date) -> dict | None:
    """Pure file read for the viewer. Returns the override OHLC dict only if a
    valid override exists whose prior_day matches `prior_day`."""
    p = _override_path(inst)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if data.get('prior_day') != str(prior_day):
        return None
    o = data.get('ohlc')
    if not o or not all(k in o for k in ('open', 'high', 'low', 'close')):
        return None
    return {
        'date': data['prior_day'],
        'open': o['open'], 'high': o['high'],
        'low': o['low'], 'close': o['close'],
        'source': 'rollover_override',
        'contract': data.get('contract'),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--inst', choices=list(INSTRUMENT_META),
                    help='single instrument (default: all)')
    ap.add_argument('--date', help='reference "today" (YYYY-MM-DD); default today')
    args = ap.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()
    insts = [args.inst] if args.inst else list(INSTRUMENT_META)
    print(f"pivot_rollover @ today={today}  insts={insts}")

    try:
        groww = _get_groww()
    except Exception as e:
        print(f"  ✗ Groww auth failed: {e}")
        return 1

    any_rolled = False
    for inst in insts:
        try:
            if maybe_backfill(inst, today, groww=groww):
                any_rolled = True
        except Exception as e:
            print(f"  [{inst}] error: {type(e).__name__}: {e}")
    print(f"  done — rollover backfill {'WROTE override(s)' if any_rolled else 'no-op'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
