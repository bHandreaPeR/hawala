"""
v3/data/fetch_option_oi_NIFTY.py
==================================
Fetch 1m candles for individual NIFTY option strikes (CE + PE)
for each historical trading day in the 1m candle cache.

Nifty specifics:
  - WEEKLY options expiring TUESDAYS (or Monday when Tuesday is a holiday)
  - Strike step: 50 pts
  - Strike band: ±1000 from open price
  - exchange='NSE', underlying_symbol='NIFTY'
  - Option symbols: NSE-NIFTY-{d.day}{d.strftime('%b')}{d.strftime('%y')}-{strike}-{side}
    e.g. NSE-NIFTY-28Apr26-24000-CE  (confirmed working against live API)

For each day:
  1. Get day's open price from futures candle cache
  2. Find nearest expiry on or after trade_date via get_expiries()
  3. Identify strikes within ±STRIKE_BAND of open (50-pt intervals)
  4. Fetch 1m candles for each strike CE + PE
  5. Store [ts, close, volume, oi, oi_raw] per side
     - oi: forward-filled (NSE publishes OI at intervals, ffill is correct)
     - oi_raw: raw OI before ffill — NaN between NSE OI publications,
               lets downstream code detect when OI actually updated vs was ffilled
     - volume: raw per-bar volume from candle index 5

Cache: v3/cache/option_oi_1m_NIFTY.pkl
Format: {date_str: {strike: {CE: DataFrame[ts, close, volume, oi, oi_raw],
                              PE: DataFrame[ts, close, volume, oi, oi_raw]}}}

Requires: valid token.env (GROWW_API_KEY + GROWW_TOTP_SECRET)
Run: python v3/data/fetch_option_oi_NIFTY.py
Each day: ~40 strikes × 2 sides = ~80 API calls × ~0.3s ≈ 25s per day
"""
import sys, time, pickle, pyotp, logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from v3.data.oi_cache_merge import merge_day_oi

CACHE_FILE   = ROOT / 'v3' / 'cache' / 'option_oi_1m_NIFTY.pkl'
CANDLE_FILE  = ROOT / 'v3' / 'cache' / 'candles_1m_NIFTY.pkl'
STRIKE_BAND  = 1000   # ± from open price
STRIKE_STEP  = 50     # Nifty 50-pt strikes
VOLUME_COLS  = True   # include volume column in stored DataFrames

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_option_oi_nifty')


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


def _option_symbol(expiry_str: str, strike: int, side: str) -> str:
    """
    Construct Groww option symbol directly using confirmed format.
    e.g. expiry_str='2026-04-28', strike=24000, side='CE'
         → 'NSE-NIFTY-28Apr26-24000-CE'
    """
    d = date.fromisoformat(expiry_str)
    return f"NSE-NIFTY-{d.strftime('%d')}{d.strftime('%b')}{d.strftime('%y')}-{strike}-{side}"


def _get_weekly_expiry_nifty(g, trade_date: date) -> str:
    """
    Return the nearest expiry on or after trade_date for NIFTY.

    Uses get_expiries(exchange='NSE', underlying_symbol='NIFTY', year, month)
    which returns weekly expiry dates. We pick the first returned date
    that is >= trade_date.
    Searches across current month + next 2 months to handle month boundaries.

    Note: NSE sometimes moves the expiry to Monday when Tuesday is a holiday
    (e.g. Mar 30 Mon due to Holi; Apr 13 Mon due to Ambedkar Jayanti).
    No weekday filter is applied — the first date >= trade_date is used.
    """
    d = trade_date
    for offset in range(3):
        m = d.month + offset
        y = d.year
        if m > 12:
            m -= 12
            y += 1
        try:
            result = g.get_expiries(
                exchange='NSE', underlying_symbol='NIFTY', year=y, month=m
            )
            for exp in sorted(result.get('expiries', [])):
                exp_d = date.fromisoformat(exp)
                if exp_d >= trade_date:
                    log.debug(
                        "NIFTY weekly expiry trade_date=%s expiry=%s weekday=%s",
                        trade_date, exp, exp_d.strftime('%a')
                    )
                    return exp
        except Exception as e:
            log.warning(
                "get_expiries NSE NIFTY year=%d month=%02d error=%s", y, m, e
            )
        time.sleep(0.3)
    raise RuntimeError(
        f"No NIFTY expiry found for trade_date={trade_date}. "
        f"Checked {d.year}-{d.month:02d} and next 2 months."
    )


def _fetch_option_oi_day(g, trade_date: date, open_price: float) -> dict:
    """
    Fetch 1m candles for all strikes ±STRIKE_BAND of open_price.
    Returns {strike: {'CE': DataFrame[ts, close, volume, oi, oi_raw],
                      'PE': DataFrame[ts, close, volume, oi, oi_raw]}}

    oi       = forward-filled OI (correct for NSE interval-published OI)
    oi_raw   = raw OI before ffill — NaN between NSE OI publications
    volume   = raw per-bar volume from candle column index 5
    """
    expiry = _get_weekly_expiry_nifty(g, trade_date)
    log.info(
        "Fetching NIFTY OI trade_date=%s open=%.0f expiry=%s",
        trade_date, open_price, expiry
    )

    base_strike = round(open_price / STRIKE_STEP) * STRIKE_STEP
    strikes = range(
        base_strike - STRIKE_BAND,
        base_strike + STRIKE_BAND + 1,
        STRIKE_STEP
    )

    start = f"{trade_date} 09:15:00"
    end   = f"{trade_date} 15:30:00"

    day_result = {}
    fetched = 0
    errors  = 0

    for strike in strikes:
        entry = {}
        for side in ['CE', 'PE']:
            sym = _option_symbol(expiry, strike, side)
            # default: empty DataFrame with all expected columns
            df_side = pd.DataFrame(columns=['ts', 'close', 'volume', 'oi', 'oi_raw'])

            for attempt in range(3):
                try:
                    r = g.get_historical_candles(
                        exchange='NSE', segment='FNO', groww_symbol=sym,
                        start_time=start, end_time=end,
                        candle_interval=g.CANDLE_INTERVAL_MIN_1,
                    )
                    candles_raw = r.get('candles', [])
                    if candles_raw:
                        ncols = len(candles_raw[0])
                        if ncols == 7:
                            cols = ['ts', 'open', 'high', 'low', 'close', 'volume', 'oi']
                        elif ncols == 6:
                            # Active contracts: Groww omits OI column
                            cols = ['ts', 'open', 'high', 'low', 'close', 'volume']
                        else:
                            raise ValueError(
                                f"Unexpected candle width={ncols} for symbol={sym} "
                                f"trade_date={trade_date}. Expected 6 or 7."
                            )
                        df = pd.DataFrame(candles_raw, columns=cols)
                        df['ts']     = pd.to_datetime(df['ts'])
                        df['close']  = pd.to_numeric(df['close'],  errors='coerce')
                        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
                        if 'oi' in df.columns:
                            df['oi_raw'] = pd.to_numeric(df['oi'], errors='coerce')
                        else:
                            # No OI in response — set to NaN so ffill propagates nothing
                            df['oi_raw'] = float('nan')
                        df['oi']    = df['oi_raw'].ffill().fillna(0)
                        df['close'] = df['close'].ffill().fillna(0)
                        df_side = df[['ts', 'close', 'volume', 'oi', 'oi_raw']].copy()
                        fetched += 1
                    break   # success (empty candles = expired/no-data, not an error)

                except Exception as e:
                    err_str = str(e)
                    is_rate_limit = 'rate limit' in err_str.lower() or 'Rate limit' in err_str
                    if is_rate_limit:
                        backoff = 10.0 * (attempt + 1)   # 10s, 20s, 30s
                        log.warning(
                            "RATE LIMIT symbol=%s trade_date=%s attempt=%d — "
                            "sleeping %.0fs then retrying",
                            sym, trade_date, attempt + 1, backoff,
                        )
                        time.sleep(backoff)
                        if attempt == 2:
                            raise RuntimeError(
                                f"Rate limit persisted after 3 attempts. "
                                f"symbol={sym} trade_date={trade_date} last_error={e}"
                            ) from e
                    else:
                        log.warning(
                            "FETCH ERROR symbol=%s trade_date=%s attempt=%d error=%s",
                            sym, trade_date, attempt + 1, e,
                        )
                        errors += 1
                        break   # non-rate-limit errors: don't retry

            entry[side] = df_side
            time.sleep(0.3)   # 0.3s between each CE/PE call (~3 req/s max)

        day_result[strike] = entry

    log.info(
        "NIFTY OI day done trade_date=%s fetched=%d errors=%d",
        trade_date, fetched, errors
    )
    return day_result


def _validate_auth(g):
    """
    Validate token is live before starting a long fetch run.
    Uses the known-good April 28, 2026 weekly expiry option.
    Raises RuntimeError with a clear message if expired.
    """
    test_sym = "NSE-NIFTY-28Apr26-24000-CE"
    start    = "2026-04-28 09:15:00"
    end      = "2026-04-28 09:17:00"
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
            f"Auth validation FAILED: Groww API error. "
            f"symbol={test_sym} error={e}. "
            "Check token.env and network connectivity."
        ) from e


def fetch_all_days(force: bool = False, max_days: int = 0, start_date=None):
    """Main entry: fetch all days in 1m candle cache.
    start_date: only process days on or after this date (date object or None for all).
    """
    if not CANDLE_FILE.exists():
        raise FileNotFoundError(
            f"NIFTY candle cache not found: {CANDLE_FILE}. "
            f"Run fetch_1m_NIFTY.py first."
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

    log.info("Trade days in NIFTY 1m cache: %d", len(day_opens))

    if CACHE_FILE.exists() and not force:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
        log.info("Existing NIFTY option OI cache: %d days", len(cache))
    else:
        cache = {}

    already_done = set(str(d) for d in cache.keys())
    g = _get_groww()
    _validate_auth(g)
    fetched_this_run = 0

    # When force=True (re-fetching for price data), iterate NEWEST-FIRST so that
    # --days=N targets the most recent N dates — the only ones likely still alive
    # on Groww (expired contracts get purged after ~30 days).
    # Normal incremental fetch keeps oldest-first to fill gaps in order.
    sort_order = force  # True = reverse (newest first), False = oldest first
    sorted_days = sorted(day_opens.items(), reverse=sort_order)

    for td, open_px in sorted_days:
        if max_days > 0 and fetched_this_run >= max_days:
            log.info("max_days=%d reached, stopping", max_days)
            break
        if not force and str(td) in already_done:
            log.info("Skip %s (cached)", td)
            continue

        day_data = _fetch_option_oi_day(g, td, open_px)
        # Merge, never overwrite: historical candles omit OI for still-active
        # contracts (oi=0). The live writers (option_flow_daemon / runners) hold
        # real OI from the option chain — preserve it rather than zeroing it.
        cache[str(td)] = merge_day_oi(cache.get(str(td)), day_data)
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
        log.info("Saved NIFTY OI date=%s strikes=%d", td, len(cache[str(td)]))
        fetched_this_run += 1
        time.sleep(1.0)

    log.info(
        "NIFTY option OI fetch done total_days=%d path=%s",
        len(cache), CACHE_FILE
    )
    return cache


if __name__ == '__main__':
    import sys
    from datetime import datetime as _dt, date as _date
    force     = '--force' in sys.argv
    max_days  = 0
    start     = None
    for arg in sys.argv[1:]:
        if arg.startswith('--days='):
            max_days = int(arg.split('=')[1])
        if arg.startswith('--from='):
            start = _dt.strptime(arg.split('=')[1], '%Y-%m-%d').date()
        if arg == '--from' and sys.argv.index(arg) + 1 < len(sys.argv):
            start = _dt.strptime(sys.argv[sys.argv.index(arg) + 1], '%Y-%m-%d').date()
    # Default for --force: only re-fetch the last 20 dates (recoverable window)
    if force and max_days == 0:
        max_days = 20
        log.info(
            "--force with no --days specified: defaulting to last %d dates. "
            "Use --days=N to override.", max_days
        )
    # Default start: Sep 1, 2025 (earliest available per Groww probe)
    if start is None and not force:
        start = _date(2025, 9, 1)
    cache = fetch_all_days(force=force, max_days=max_days, start_date=start)
    print(f"\nNIFTY Option OI cache: {len(cache)} days")
    if cache:
        last_d = max(cache.keys())
        day_data = cache[last_d]
        strikes = sorted(day_data.keys())
        print(f"Last day: {last_d}, strikes: {len(strikes)}, "
              f"range: {strikes[0]}-{strikes[-1]}")
        mid = strikes[len(strikes) // 2]
        ce_df = day_data[mid]['CE']
        print(f"Sample strike {mid} CE: {len(ce_df)} candles, "
              f"columns: {list(ce_df.columns)}")
