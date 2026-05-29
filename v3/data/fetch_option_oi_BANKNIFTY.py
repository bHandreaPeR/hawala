"""
v3/data/fetch_option_oi_BANKNIFTY.py
=====================================
Fetch 1m candles for individual BankNifty option strikes (CE + PE)
for each historical trading day in the 1m candle cache.

BankNifty specifics:
  - MONTHLY options expiring LAST TUESDAY of each month
  - Strike step: 100 pts
  - Strike band: ±2000 from open price
  - Exchange: NSE, Underlying: BANKNIFTY
  - Symbol format: NSE-BANKNIFTY-{day}{Mon}{YY}-{strike}-{side}
    e.g. NSE-BANKNIFTY-28Apr26-56000-CE

Groww stores expired contract data — can fetch full Jan–Apr 2026 history.
Run with --force to re-fetch all days, or incremental (default) to add new days.

For each day:
  1. Get day's open price from futures candle cache
  2. Find nearest monthly expiry via _get_near_monthly_expiry()
  3. Identify strikes within ±STRIKE_BAND of open (100-pt intervals)
  4. Fetch 1m candles for each strike CE + PE
  5. Store [ts, close, volume, oi, oi_raw] per side

Cache: v3/cache/option_oi_1m_BANKNIFTY.pkl
Format: {date_str: {strike: {CE: DataFrame[ts, close, volume, oi, oi_raw],
                              PE: DataFrame[ts, close, volume, oi, oi_raw]}}}
"""
import sys, time, pickle, pyotp, logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from growwapi.groww.exceptions import GrowwAPIRateLimitException, GrowwAPIAuthenticationException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from v3.data.oi_cache_merge import merge_day_oi

CACHE_FILE  = ROOT / 'v3' / 'cache' / 'option_oi_1m_BANKNIFTY.pkl'
CANDLE_FILE = ROOT / 'v3' / 'cache' / 'candles_1m_BANKNIFTY.pkl'
STRIKE_BAND = 2000    # ± from open price
STRIKE_STEP = 100     # BankNifty 100-pt strikes

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_option_oi_banknifty')


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
    """Re-authenticate with a fresh TOTP when access token expires mid-run."""
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
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - 1) % 7
    return last_day - timedelta(days=days_back)


def _get_near_monthly_expiry(trade_date: date) -> date:
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
        f"Cannot find BankNifty monthly expiry for trade_date={trade_date}"
    )


def _option_symbol(expiry: date, strike: int, side: str) -> str:
    """date(2026,4,28), 56000, 'CE' → 'NSE-BANKNIFTY-28Apr26-56000-CE'"""
    return (
        f"NSE-BANKNIFTY-"
        f"{expiry.strftime('%d')}{expiry.strftime('%b')}{expiry.strftime('%y')}"
        f"-{strike}-{side}"
    )


# ── Single-strike fetch ───────────────────────────────────────────────────────
RATE_LIMIT_BACKOFF = [30, 60, 120]  # seconds to wait on successive 429s

def _fetch_strike(g, trade_date: date, expiry: date,
                  strike: int, side: str) -> pd.DataFrame:
    symbol = _option_symbol(expiry, strike, side)
    start  = f"{trade_date} 09:15:00"
    end    = f"{trade_date} 15:30:00"

    for attempt, backoff in enumerate(RATE_LIMIT_BACKOFF + [None], start=1):
        try:
            r = g.get_historical_candles(
                exchange='NSE', segment='FNO', groww_symbol=symbol,
                start_time=start, end_time=end,
                candle_interval=g.CANDLE_INTERVAL_MIN_1
            )
            candles = r.get('candles', [])
            if not candles:
                return pd.DataFrame()
            ncols = len(candles[0])
            if ncols == 7:
                cols = ['ts', 'open', 'high', 'low', 'close', 'volume', 'oi_raw']
            elif ncols == 6:
                cols = ['ts', 'open', 'high', 'low', 'close', 'volume']
            else:
                raise ValueError(
                    f"Unexpected candle width={ncols} for symbol={symbol} "
                    f"trade_date={trade_date}. Expected 6 or 7."
                )
            df = pd.DataFrame(candles, columns=cols)
            df['ts'] = pd.to_datetime(df['ts'])
            df['close']  = pd.to_numeric(df['close'],  errors='coerce')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
            if 'oi_raw' in df.columns:
                df['oi_raw'] = pd.to_numeric(df['oi_raw'], errors='coerce')
            else:
                df['oi_raw'] = float('nan')
            df['oi'] = df['oi_raw'].ffill().fillna(0)
            df['close'] = df['close'].ffill().fillna(0)
            return df[['ts', 'close', 'volume', 'oi', 'oi_raw']].reset_index(drop=True)

        except GrowwAPIRateLimitException:
            if backoff is None:
                log.error(
                    "Rate limit persists after %d attempts — skip strike=%d %s %s %s",
                    attempt - 1, strike, side, trade_date, symbol
                )
                return pd.DataFrame()
            log.warning(
                "Rate limited (attempt %d) — backing off %ds  strike=%d %s %s",
                attempt, backoff, strike, side, trade_date
            )
            time.sleep(backoff)

        except GrowwAPIAuthenticationException:
            raise  # let caller handle token refresh

        except Exception as e:
            log.warning(
                "skip strike=%d %s %s %s: %s", strike, side, trade_date, symbol, e
            )
            return pd.DataFrame()


# ── Single-day fetch ──────────────────────────────────────────────────────────
def _fetch_option_oi_day(g, trade_date: date, open_px: float) -> dict:
    expiry  = _get_near_monthly_expiry(trade_date)
    lo      = int(round((open_px - STRIKE_BAND) / STRIKE_STEP) * STRIKE_STEP)
    hi      = int(round((open_px + STRIKE_BAND) / STRIKE_STEP) * STRIKE_STEP)
    strikes = list(range(lo, hi + STRIKE_STEP, STRIKE_STEP))

    log.info(
        "  %s: expiry=%s open=%.0f strikes=%d (%d–%d)",
        trade_date, expiry, open_px, len(strikes), strikes[0], strikes[-1]
    )

    day_data = {}
    for strike in strikes:
        ce_df = _fetch_strike(g, trade_date, expiry, strike, 'CE')
        pe_df = _fetch_strike(g, trade_date, expiry, strike, 'PE')
        if not ce_df.empty or not pe_df.empty:
            day_data[strike] = {'CE': ce_df, 'PE': pe_df}
        time.sleep(0.4)   # 0.15 → 0.4 to stay under rate limit

    return day_data


# ── Auth validation ───────────────────────────────────────────────────────────
def _validate_auth(g):
    test_sym = "NSE-BANKNIFTY-28Apr26-56000-CE"
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
def fetch_all_days(start_date: date = None, force: bool = False, max_days: int = 0):
    """
    Fetch option OI for all days in the futures candle cache.
    - start_date: only process days on/after this date (default: process all)
    - force: re-fetch even if already cached
    - max_days: stop after this many new fetches (0 = unlimited)
    """
    if not CANDLE_FILE.exists():
        raise FileNotFoundError(
            f"BankNifty candle cache not found: {CANDLE_FILE}. "
            "Run fetch_1m_BANKNIFTY.py first."
        )

    with open(CANDLE_FILE, 'rb') as f:
        candles = pickle.load(f)

    day_opens = {}
    for td in sorted(candles['date'].unique()):
        if start_date and td < start_date:
            continue
        day_df = candles[candles['date'] == td].sort_values('ts')
        if not day_df.empty:
            day_opens[td] = float(day_df['open'].iloc[0])

    log.info("Trade days to process: %d", len(day_opens))

    if CACHE_FILE.exists() and not force:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
        log.info("Existing BankNifty option OI cache: %d days", len(cache))
    else:
        cache = {}

    already_done = set(str(d) for d in cache.keys())
    g = _get_groww()
    _validate_auth(g)
    fetched = 0

    # Newest-first on force (most recent data still available), oldest-first normally
    sorted_days = sorted(day_opens.items(), reverse=force)

    for td, open_px in sorted_days:
        if max_days > 0 and fetched >= max_days:
            log.info("max_days=%d reached, stopping", max_days)
            break
        if not force and str(td) in already_done:
            continue

        log.info("Fetching BankNifty option OI %s (open=%.0f) ...", td, open_px)
        try:
            day_data = _fetch_option_oi_day(g, td, open_px)
        except Exception as exc:
            if 'Authentication failed' in str(exc) or 'expired' in str(exc).lower():
                log.warning("Token expired — refreshing and retrying %s ...", td)
                g        = _refresh_groww()
                day_data = _fetch_option_oi_day(g, td, open_px)
            else:
                raise
        # Merge, never overwrite — historical candles omit OI for active
        # contracts; preserve real OI written live by the daemon/runners.
        cache[str(td)] = merge_day_oi(cache.get(str(td)), day_data)
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
        log.info(
            "Saved BANKNIFTY OI date=%s strikes=%d", td, len(cache[str(td)])
        )
        fetched += 1
        time.sleep(1.5)   # 0.5 → 1.5 between days

    log.info(
        "BankNifty option OI fetch done total_days=%d path=%s",
        len(cache), CACHE_FILE
    )
    return cache


if __name__ == '__main__':
    import sys
    force    = '--force' in sys.argv
    max_days = 0
    start    = None

    for arg in sys.argv[1:]:
        if arg.startswith('--days='):
            max_days = int(arg.split('=')[1])
        if arg.startswith('--from='):
            from datetime import datetime
            start = datetime.strptime(arg.split('=')[1], '%Y-%m-%d').date()
        if arg == '--from' and sys.argv.index(arg) + 1 < len(sys.argv):
            # Handle: --from 2025-07-01 (space-separated)
            from datetime import datetime
            start = datetime.strptime(sys.argv[sys.argv.index(arg) + 1], '%Y-%m-%d').date()

    # Default: start from Jan 2, 2026
    # Pass --from=YYYY-MM-DD to expand further back (e.g. --from=2025-07-01)
    if start is None:
        start = date(2026, 1, 2)

    if force and max_days == 0:
        max_days = 30
        log.info("--force without --days: defaulting to last %d dates", max_days)

    cache = fetch_all_days(start_date=start, force=force, max_days=max_days)
    print(f"\nBankNifty Option OI cache: {len(cache)} days")
    if cache:
        last_d = max(cache.keys())
        day_data = cache[last_d]
        strikes  = sorted(day_data.keys())
        print(f"Last day: {last_d}, strikes: {len(strikes)}, range: {strikes[0]}–{strikes[-1]}")
        if strikes:
            mid = strikes[len(strikes) // 2]
            ce_df = day_data[mid].get('CE', pd.DataFrame())
            print(f"Sample {mid} CE: {len(ce_df)} candles, cols: {list(ce_df.columns)}")
