"""
v3/data/fetch_1m_BANKNIFTY.py
==============================
Fetch & cache 1m BankNifty futures candles from Groww.

BankNifty specifics:
  - Monthly futures expiry: LAST TUESDAY of each month.
  - Symbol format: NSE-BANKNIFTY-{d.day}{d.strftime('%b')}{d.strftime('%y')}-FUT
    e.g. NSE-BANKNIFTY-28Apr26-FUT
  - Exchange: NSE, Segment: FNO

Groww API returns expired contract data — run with lookback_days=120 to
fetch Jan–Apr 2026 in one shot.

Cache: v3/cache/candles_1m_BANKNIFTY.pkl
Format: DataFrame[ts, open, high, low, close, volume, oi, date, time]
"""
import sys, pickle, time, pyotp, logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CACHE_FILE = ROOT / 'v3' / 'cache' / 'candles_1m_BANKNIFTY.pkl'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_1m_banknifty')


# ── Auth ──────────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    with open(ROOT / 'token.env') as f:
        for line in f:
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k] = v
    return env

def _get_groww():
    from growwapi import GrowwAPI
    env   = _load_env()
    totp  = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
    return GrowwAPI(token=token)

def _refresh_groww(retries: int = 3) -> object:
    """Re-authenticate with a fresh TOTP. Used when access token expires mid-run."""
    from growwapi import GrowwAPI
    import time as _time
    env = _load_env()
    for attempt in range(1, retries + 1):
        try:
            totp  = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            g     = GrowwAPI(token=token)
            log.info("Token refreshed OK (attempt %d)", attempt)
            return g
        except Exception as e:
            log.warning("Token refresh attempt %d failed: %s", attempt, e)
            if attempt < retries:
                _time.sleep(2)
    raise RuntimeError(
        "Token refresh failed after %d attempts. "
        "Check GROWW_TOTP_SECRET in token.env." % retries
    )


# ── Contract resolver ─────────────────────────────────────────────────────────
def _last_tuesday(year: int, month: int) -> date:
    """Return last Tuesday of the given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - 1) % 7   # weekday 1 = Tuesday
    return last_day - timedelta(days=days_back)


def _get_near_monthly_expiry(trade_date: date) -> date:
    """
    Return nearest BankNifty monthly futures expiry (last Tuesday) on or after trade_date.
    Includes known holiday overrides.
    """
    EXPIRY_OVERRIDES = {
        date(2026, 3, 31): date(2026, 3, 30),   # Mar 2026: last-Tue=31 is holiday → 30
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
        f"Cannot find BankNifty monthly futures expiry for trade_date={trade_date}"
    )


def _expiry_to_symbol(expiry: date) -> str:
    """date(2026, 4, 28) → 'NSE-BANKNIFTY-28Apr26-FUT'"""
    return f"NSE-BANKNIFTY-{expiry.day}{expiry.strftime('%b')}{expiry.strftime('%y')}-FUT"


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
        # Paise normalisation: BankNifty valid range 30,000–100,000
        # Groww occasionally returns next-month contract prices in paise
        for col in ('open', 'high', 'low', 'close'):
            if df[col].median() > 100_000:
                log.warning(
                    "Paise detected in %s %s — dividing by 100", col, trade_date
                )
                df[col] = df[col] / 100.0
        return df
    except Exception as e:
        log.error(
            "fetch_day_1m trade_date=%s symbol=%s error=%s", trade_date, symbol, e
        )
        raise RuntimeError(
            f"Failed to fetch BANKNIFTY 1m candles: trade_date={trade_date} "
            f"symbol={symbol} error={e}"
        ) from e


# ── Auth validation ───────────────────────────────────────────────────────────
def _validate_auth(g):
    test_sym = "NSE-BANKNIFTY-28Apr26-FUT"
    start, end = "2026-04-28 09:15:00", "2026-04-28 09:17:00"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='FNO', groww_symbol=test_sym,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', [])
        if not candles:
            raise RuntimeError(
                f"Auth validation FAILED: {test_sym} returned 0 candles. "
                "Re-generate token.env with a fresh TOTP."
            )
        log.info("Auth validated OK — %d candles from %s", len(candles), test_sym)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Auth validation FAILED: Groww API error — symbol={test_sym} error={e}"
        ) from e


# ── Main fetch loop ───────────────────────────────────────────────────────────
def fetch_and_cache(start_date: date = None, lookback_days: int = 120,
                    force_full: bool = False):
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
    if start_date is not None:
        date_range = [start_date + timedelta(days=i)
                      for i in range((today - start_date).days + 1)]
    else:
        date_range = [today - timedelta(days=i) for i in range(lookback_days, -1, -1)]

    new_frames = []
    fetched = 0

    for d in date_range:
        if d.weekday() >= 5:          # skip weekends
            continue
        if d in cached_dates:
            continue
        if d > today:
            continue
        # Skip today pre-market (Groww rejects future timestamps)
        from datetime import datetime as _dt, time as _tm
        if d == today and _dt.now().time() < _tm(15, 30):
            continue

        log.info("Fetching BANKNIFTY 1m %s ...", d)
        try:
            df_day = _fetch_day_1m(g, d)
        except RuntimeError as exc:
            # Auto-refresh on token expiry and retry once
            if 'Authentication failed' in str(exc) or 'expired' in str(exc).lower():
                log.warning("Token expired — refreshing and retrying %s ...", d)
                g      = _refresh_groww()
                df_day = _fetch_day_1m(g, d)   # raises if still failing
            else:
                raise

        if not df_day.empty:
            new_frames.append(df_day)
            fetched += 1
            log.info("  %s: %d candles (total_new=%d)", d, len(df_day), fetched)
            combined = pd.concat([existing] + new_frames, ignore_index=True)
            combined.drop_duplicates(subset=['ts'], inplace=True)
            combined.sort_values('ts', inplace=True)
            combined.reset_index(drop=True, inplace=True)
            with open(CACHE_FILE, 'wb') as f:
                pickle.dump(combined, f)
        else:
            log.info("  %s: no data (holiday/gap)", d)
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
    # Default: fetch Jan 2, 2026 → today (full Jan–Apr history)
    start = date(2026, 1, 2)
    if '--from' in sys.argv:
        idx = sys.argv.index('--from')
        from datetime import datetime
        start = datetime.strptime(sys.argv[idx + 1], '%Y-%m-%d').date()

    force = '--force' in sys.argv
    df = fetch_and_cache(start_date=start, force_full=force)
    print(f"\nBankNifty 1m cache: {len(df)} candles")
    if not df.empty:
        print(f"Date range: {df['date'].min()} → {df['date'].max()}")
        print(f"Unique days: {df['date'].nunique()}")
