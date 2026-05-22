"""
v3/data/fetch_1m_NIFTY.py
==========================
Fetch & cache 1m NIFTY futures candles from Groww.
Window: last ~30 trading days (API limit per request). Run daily to stay current.

Nifty futures expiry: LAST THURSDAY of each month (monthly contract).
  - Do NOT use get_expiries() — that returns Tuesday OPTION expiries, not futures.
  - e.g. April 2026 futures → 30Apr26, not 28Apr26.

Cache: v3/cache/candles_1m_NIFTY.pkl
Format: DataFrame[ts, open, high, low, close, volume, oi, date, time]
"""
import os, sys, pickle, time, pyotp, logging
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CACHE_FILE = ROOT / 'v3' / 'cache' / 'candles_1m_NIFTY.pkl'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_1m_nifty')


# ── Auth ──────────────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    env = {}
    with open(ROOT / 'token.env') as f:
        for line in f:
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k] = v
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
    return GrowwAPI(token=token)


# ── Contract resolver ─────────────────────────────────────────────────────────
def _last_tuesday(year: int, month: int) -> date:
    """
    Return last Tuesday of the given month.
    NSE Nifty monthly futures expire on the LAST TUESDAY of each month
    (same day as Nifty weekly options — both moved to Tuesday from Thursday).
    """
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    # weekday 1 = Tuesday
    days_back = (last_day.weekday() - 1) % 7
    return last_day - timedelta(days=days_back)


def _get_near_monthly_expiry(trade_date: date) -> date:
    """
    Return nearest NIFTY monthly futures expiry (last Tuesday) on or after trade_date.
    Does NOT use get_expiries() — computes directly.
    Nifty futures expire on last Tuesday of each month.

    EXPIRY_OVERRIDES: calendar last-Tuesday is sometimes a market holiday —
    NSE moves expiry to the prior trading day. Confirmed overrides:
      - Mar 2026: calendar last-Tue = Mar 31 (holiday) → actual expiry = Mar 30
        Symbol: NSE-NIFTY-30Mar26-FUT  (NOT 31Mar26)
    """
    EXPIRY_OVERRIDES = {
        date(2026, 3, 31): date(2026, 3, 30),
    }
    y, m = trade_date.year, trade_date.month
    for _ in range(3):
        exp = _last_tuesday(y, m)
        exp = EXPIRY_OVERRIDES.get(exp, exp)
        if exp >= trade_date:
            return exp
        if m == 12:
            m, y = 1, y + 1
        else:
            m += 1
    raise RuntimeError(
        f"Cannot find NIFTY monthly futures expiry for trade_date={trade_date}"
    )


def _expiry_to_symbol(expiry: date) -> str:
    """date(2026, 4, 28) → 'NSE-NIFTY-28Apr26-FUT'"""
    return f"NSE-NIFTY-{expiry.day}{expiry.strftime('%b')}{expiry.strftime('%y')}-FUT"


# ── Single-day fetch ──────────────────────────────────────────────────────────
def _fetch_day_1m(g, trade_date: date) -> pd.DataFrame:
    expiry = _get_near_monthly_expiry(trade_date)
    symbol = _expiry_to_symbol(expiry)
    start  = f"{trade_date} 09:15:00"
    end    = f"{trade_date} 15:30:00"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='FNO', groww_symbol=symbol,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1
        )
        candles = r.get('candles', [])
        if not candles:
            return pd.DataFrame()
        # Groww returns 7 cols for expired contracts (has OI), 6 for active (no OI field)
        n_cols = len(candles[0])
        if n_cols >= 7:
            df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        elif n_cols == 6:
            df = pd.DataFrame(candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            df['oi'] = float('nan')
        else:
            log.warning(
                "Unexpected candle column count=%d for %s %s — skipping",
                n_cols, trade_date, symbol
            )
            return pd.DataFrame()
        df['ts']   = pd.to_datetime(df['ts'])
        df['date'] = df['ts'].dt.date
        df['time'] = df['ts'].dt.time
        df[['open', 'high', 'low', 'close', 'volume']] = \
            df[['open', 'high', 'low', 'close', 'volume']].apply(
                pd.to_numeric, errors='coerce'
            )
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').ffill()
        return df
    except Exception as e:
        log.error(
            "fetch_day_1m trade_date=%s symbol=%s error=%s", trade_date, symbol, e
        )
        raise RuntimeError(
            f"Failed to fetch NIFTY 1m candles: trade_date={trade_date} "
            f"symbol={symbol} error={e}"
        ) from e


# ── Main fetch loop ───────────────────────────────────────────────────────────
def _validate_auth(g):
    """
    Validate that the Groww token is live by fetching a single known candle.
    Raises RuntimeError with clear message if token is expired or auth fails.
    """
    from datetime import date, timedelta
    # Use a recent known trading day: April 28, 2026 (last Tuesday = Nifty expiry)
    test_date = date(2026, 4, 28)
    test_sym  = "NSE-NIFTY-28Apr26-FUT"
    start     = f"{test_date} 09:15:00"
    end       = f"{test_date} 09:17:00"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='FNO', groww_symbol=test_sym,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', [])
        if not candles:
            raise RuntimeError(
                "Auth validation FAILED: Groww token appears expired or invalid. "
                f"Test symbol={test_sym} returned 0 candles. "
                "Re-generate your token.env with a fresh TOTP and retry."
            )
        log.info("Auth validated OK — test fetch returned %d candles", len(candles))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Auth validation FAILED: Groww API error on test fetch. "
            f"symbol={test_sym} error={e}. "
            "Check token.env and network connectivity."
        ) from e


def fetch_and_cache(lookback_days: int = 35, force_full: bool = False,
                    start_date: date = None):
    """
    Fetch 1m candles.
    - If start_date provided: fetch from start_date to today.
    - Else: fetch last lookback_days calendar days.
    Skips dates already in cache unless force_full=True.
    """
    g = _get_groww()
    _validate_auth(g)

    if CACHE_FILE.exists() and not force_full:
        with open(CACHE_FILE, 'rb') as f:
            existing = pickle.load(f)
        cached_dates = set(existing['date'].unique()) if not existing.empty else set()
    else:
        existing = pd.DataFrame()
        cached_dates = set()

    log.info("Cached dates: %d", len(cached_dates))

    today = date.today()
    new_frames = []
    fetched = 0

    # Build date list — either from start_date or last lookback_days
    if start_date is not None:
        date_list = [start_date + timedelta(days=i)
                     for i in range((today - start_date).days + 1)]
    else:
        date_list = [today - timedelta(days=i) for i in range(lookback_days, -1, -1)]

    for d in date_list:
        if d.weekday() >= 5:
            continue
        if d in cached_dates:
            continue
        if d > today:
            continue
        # Skip today pre-market (Groww rejects future timestamps)
        from datetime import datetime as _dt, time as _tm
        if d == today and _dt.now().time() < _tm(15, 30):
            continue

        log.info("Fetching NIFTY 1m %s ...", d)
        try:
            df_day = _fetch_day_1m(g, d)
        except RuntimeError as exc:
            if 'Authentication failed' in str(exc) or 'expired' in str(exc).lower():
                log.warning("Token expired — refreshing and retrying %s ...", d)
                from v3.data.fetch_1m_BANKNIFTY import _refresh_groww
                g      = _refresh_groww()
                df_day = _fetch_day_1m(g, d)
            else:
                log.error("Error fetching %s: %s — skipping", d, exc)
                df_day = pd.DataFrame()
        if not df_day.empty:
            new_frames.append(df_day)
            fetched += 1
            log.info("  %s: %d candles  (total new=%d)", d, len(df_day), fetched)
            # Incremental save — preserves progress if process is killed
            combined = pd.concat([existing] + new_frames, ignore_index=True)
            combined.drop_duplicates(subset=['ts'], inplace=True)
            combined.sort_values('ts', inplace=True)
            combined.reset_index(drop=True, inplace=True)
            with open(CACHE_FILE, 'wb') as f:
                pickle.dump(combined, f)
        else:
            log.info("  %s: no data (holiday/market closed/Groww gap)", d)
        time.sleep(0.4)

    if not new_frames:
        log.info("Nothing new to add.")
        return existing

    combined = pd.concat([existing] + new_frames, ignore_index=True)
    combined.drop_duplicates(subset=['ts'], inplace=True)
    combined.sort_values('ts', inplace=True)
    combined.reset_index(drop=True, inplace=True)

    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(combined, f)

    log.info(
        "Cache updated: total_candles=%d new_days=%d path=%s",
        len(combined), fetched, CACHE_FILE
    )
    return combined


if __name__ == '__main__':
    import sys
    start = None
    force = '--force' in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith('--from='):
            from datetime import datetime as _dt
            start = _dt.strptime(arg.split('=')[1], '%Y-%m-%d').date()
        elif arg == '--from' and sys.argv.index(arg) + 1 < len(sys.argv):
            from datetime import datetime as _dt
            start = _dt.strptime(sys.argv[sys.argv.index(arg) + 1], '%Y-%m-%d').date()

    if start is None:
        start = date(2025, 9, 1)   # default: expand from Sep 2025

    df = fetch_and_cache(start_date=start, force_full=force)
    print(f"\nTotal 1m candles: {len(df)}")
    if not df.empty:
        print(f"Date range: {df['date'].min()} → {df['date'].max()}")
        print(f"Unique days: {df['date'].nunique()}")
