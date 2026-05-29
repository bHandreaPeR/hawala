"""
v3/data/fetch_option_oi_SENSEX.py
===================================
Fetch 1m candles for individual SENSEX option strikes (CE + PE)
for each historical trading day in the 1m candle cache.

Sensex specifics:
  - BSE exchange, WEEKLY options expiring THURSDAYS
  - Strike step: 100 pts
  - Strike band: ±1500 from open price
  - exchange='BSE', underlying_symbol='SENSEX'
  - Option symbols: BSE-SENSEX-{day}{Mon}{YY}-{strike}-CE

For each day:
  1. Get day's open price from futures candle cache
  2. Find nearest THURSDAY expiry on or after trade_date via get_expiries()
  3. Identify strikes within ±STRIKE_BAND of open (100-pt intervals)
  4. Fetch 1m candles for each strike CE + PE
  5. Extract OI column per 1m candle

Cache: v3/cache/option_oi_1m_SENSEX.pkl
Format: {date_str: {strike: {CE: DataFrame[ts, oi], PE: DataFrame[ts, oi]}}}

Requires: valid token.env (GROWW_API_KEY + GROWW_TOTP_SECRET)
Run: python v3/data/fetch_option_oi_SENSEX.py
"""
import sys, time, pickle, pyotp, logging
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from v3.data.oi_cache_merge import merge_day_oi

CACHE_FILE  = ROOT / 'v3' / 'cache' / 'option_oi_1m_SENSEX.pkl'
CANDLE_FILE = ROOT / 'v3' / 'cache' / 'candles_1m_SENSEX.pkl'
STRIKE_BAND = 1500   # ± from open price
STRIKE_STEP = 100    # Sensex 100-pt strikes

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_option_oi_sensex')


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


def _get_weekly_expiry_sensex(g, trade_date: date) -> str:
    """
    Return the nearest THURSDAY expiry on or after trade_date for SENSEX.

    Uses get_expiries(exchange='BSE', underlying_symbol='SENSEX', year, month)
    which returns weekly expiry dates. We pick the first returned date
    that is a Thursday (weekday=3) and is >= trade_date.
    Searches across current month + next 2 months to handle month boundaries.
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
                exchange='BSE', underlying_symbol='SENSEX', year=y, month=m
            )
            for exp in sorted(result.get('expiries', [])):
                exp_d = date.fromisoformat(exp)
                if exp_d >= trade_date and exp_d.weekday() == 3:  # Thursday
                    log.debug(
                        "SENSEX weekly expiry trade_date=%s expiry=%s",
                        trade_date, exp
                    )
                    return exp
        except Exception as e:
            log.warning(
                "get_expiries BSE SENSEX year=%d month=%02d error=%s", y, m, e
            )
        time.sleep(0.3)
    raise RuntimeError(
        f"No SENSEX Thursday expiry found for trade_date={trade_date}. "
        f"Checked {d.year}-{d.month:02d} and next 2 months."
    )


_SYMBOL_FORMAT: str = None


def _candidate_symbols(expiry_str: str, strike: int, side: str) -> list:
    """All plausible Groww option symbol formats to probe."""
    d = date.fromisoformat(expiry_str)
    day_bare   = str(d.day)
    day_padded = d.strftime('%d')
    mon_title  = d.strftime('%b')
    mon_upper  = d.strftime('%b').upper()
    yr_short   = d.strftime('%y')
    yr_long    = str(d.year)
    return [
        f"BSE-SENSEX-{day_bare}{mon_title}{yr_short}-{strike}-{side}",
        f"BSE-SENSEX-{day_padded}{mon_title}{yr_short}-{strike}-{side}",
        f"BSE-SENSEX-{day_bare}{mon_upper}{yr_long}-{strike}-{side}",
        f"BSE-SENSEX-{day_padded}{mon_upper}{yr_long}-{strike}-{side}",
        f"BSE-SENSEX-{day_bare}{mon_upper}{yr_short}-{strike}-{side}",
        f"BSE-SENSEX-{day_padded}{mon_upper}{yr_short}-{strike}-{side}",
    ]


def _option_symbol(expiry_str: str, strike: int, side: str) -> str:
    """Return option symbol in the detected/default Groww format."""
    global _SYMBOL_FORMAT
    d = date.fromisoformat(expiry_str)
    if _SYMBOL_FORMAT is None:
        _SYMBOL_FORMAT = "{day}{mon_title}{yr_short}"
    day   = str(d.day)
    mon_t = d.strftime('%b')
    mon_u = d.strftime('%b').upper()
    yr_s  = d.strftime('%y')
    yr_l  = str(d.year)
    fmt   = (_SYMBOL_FORMAT
             .replace('{day}',       day)
             .replace('{day_pad}',   d.strftime('%d'))
             .replace('{mon_title}', mon_t)
             .replace('{mon_upper}', mon_u)
             .replace('{yr_short}',  yr_s)
             .replace('{yr_long}',   yr_l))
    return f"BSE-SENSEX-{fmt}-{strike}-{side}"


def _probe_symbol_format(g, expiry_str: str, strike: int,
                          trade_date: date) -> str | None:
    """
    Try each candidate symbol format until one returns candles.
    Returns the format template string, or None if all fail.
    """
    global _SYMBOL_FORMAT
    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T09:30:00"

    for sym in _candidate_symbols(expiry_str, strike, 'CE'):
        try:
            r = g.get_historical_candles(
                exchange='BSE', segment='FNO', groww_symbol=sym,
                start_time=start, end_time=end,
                candle_interval=g.CANDLE_INTERVAL_MIN_1,
            )
            if r.get('candles'):
                date_part = sym.split('-')[2]
                d = date.fromisoformat(expiry_str)
                templates = [
                    ("{day}{mon_title}{yr_short}",
                     f"{d.day}{d.strftime('%b')}{d.strftime('%y')}"),
                    ("{day_pad}{mon_title}{yr_short}",
                     f"{d.strftime('%d')}{d.strftime('%b')}{d.strftime('%y')}"),
                    ("{day}{mon_upper}{yr_long}",
                     f"{d.day}{d.strftime('%b').upper()}{d.year}"),
                    ("{day_pad}{mon_upper}{yr_long}",
                     f"{d.strftime('%d')}{d.strftime('%b').upper()}{d.year}"),
                    ("{day}{mon_upper}{yr_short}",
                     f"{d.day}{d.strftime('%b').upper()}{d.strftime('%y')}"),
                    ("{day_pad}{mon_upper}{yr_short}",
                     f"{d.strftime('%d')}{d.strftime('%b').upper()}{d.strftime('%y')}"),
                ]
                for tmpl, val in templates:
                    if val == date_part:
                        _SYMBOL_FORMAT = tmpl
                        log.info(
                            "SENSEX symbol format detected: symbol=%s template=%s",
                            sym, tmpl
                        )
                        return tmpl
        except Exception as e:
            log.debug("Probe symbol=%s error=%s", sym, e)
        time.sleep(0.1)

    log.error(
        "Could not detect SENSEX option symbol format — all candidates failed "
        "expiry=%s strike=%d trade_date=%s", expiry_str, strike, trade_date
    )
    return None


def _fetch_option_oi_day(g, trade_date: date, open_price: float) -> dict:
    """
    Fetch 1m OI for all strikes ±STRIKE_BAND of open_price.
    Returns {strike: {'CE': DataFrame[ts,oi], 'PE': DataFrame[ts,oi]}}
    """
    global _SYMBOL_FORMAT
    expiry = _get_weekly_expiry_sensex(g, trade_date)
    log.info(
        "Fetching SENSEX OI trade_date=%s open=%.0f expiry=%s",
        trade_date, open_price, expiry
    )

    if _SYMBOL_FORMAT is None:
        base_probe = round(open_price / STRIKE_STEP) * STRIKE_STEP
        fmt = _probe_symbol_format(g, expiry, base_probe, trade_date)
        if fmt is None:
            raise RuntimeError(
                f"Cannot fetch SENSEX option OI for {trade_date}: "
                f"symbol format unknown, all probe attempts failed. "
                f"expiry={expiry} open={open_price:.0f}"
            )
        time.sleep(0.3)

    base_strike = round(open_price / STRIKE_STEP) * STRIKE_STEP
    strikes = range(
        base_strike - STRIKE_BAND,
        base_strike + STRIKE_BAND + 1,
        STRIKE_STEP
    )

    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T15:30:00"

    day_result = {}
    fetched = 0
    errors  = 0

    for strike in strikes:
        entry = {}
        for side in ['CE', 'PE']:
            sym = _option_symbol(expiry, strike, side)
            df_side = pd.DataFrame(columns=['ts', 'oi'])   # default: empty

            for attempt in range(3):
                try:
                    r = g.get_historical_candles(
                        exchange='BSE', segment='FNO', groww_symbol=sym,
                        start_time=start, end_time=end,
                        candle_interval=g.CANDLE_INTERVAL_MIN_1,
                    )
                    candles_raw = r.get('candles', [])
                    if candles_raw:
                        df = pd.DataFrame(
                            candles_raw,
                            columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'oi']
                        )
                        df['ts'] = pd.to_datetime(df['ts'])
                        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').ffill().fillna(0)
                        df_side = df[['ts', 'oi']].copy()
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
        "SENSEX OI day done trade_date=%s fetched=%d errors=%d",
        trade_date, fetched, errors
    )
    return day_result


def fetch_all_days(force: bool = False, max_days: int = 0):
    """Main entry: fetch all days in SENSEX 1m candle cache."""
    global _SYMBOL_FORMAT
    _SYMBOL_FORMAT = None

    if not CANDLE_FILE.exists():
        raise FileNotFoundError(
            f"SENSEX candle cache not found: {CANDLE_FILE}. "
            f"Run fetch_1m_SENSEX.py first."
        )

    with open(CANDLE_FILE, 'rb') as f:
        candles = pickle.load(f)

    day_opens = {}
    for td in sorted(candles['date'].unique()):
        day_df = candles[candles['date'] == td].sort_values('ts')
        if not day_df.empty:
            day_opens[td] = float(day_df['open'].iloc[0])

    log.info("Trade days in SENSEX 1m cache: %d", len(day_opens))

    if CACHE_FILE.exists() and not force:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
        log.info("Existing SENSEX option OI cache: %d days", len(cache))
    else:
        cache = {}

    already_done = set(str(d) for d in cache.keys())
    g = _get_groww()
    fetched_this_run = 0

    for td, open_px in sorted(day_opens.items()):
        if max_days > 0 and fetched_this_run >= max_days:
            log.info("max_days=%d reached, stopping", max_days)
            break
        if str(td) in already_done:
            log.info("Skip %s (cached)", td)
            continue

        day_data = _fetch_option_oi_day(g, td, open_px)
        # Merge, never overwrite — historical candles omit OI for active
        # contracts; preserve real OI written live by the daemon.
        cache[str(td)] = merge_day_oi(cache.get(str(td)), day_data)
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
        log.info("Saved SENSEX OI date=%s strikes=%d", td, len(cache[str(td)]))
        fetched_this_run += 1
        time.sleep(1.0)

    log.info(
        "SENSEX option OI fetch done total_days=%d path=%s",
        len(cache), CACHE_FILE
    )
    return cache


if __name__ == '__main__':
    import sys
    force = '--force' in sys.argv
    max_days = 0
    for arg in sys.argv[1:]:
        if arg.startswith('--days='):
            max_days = int(arg.split('=')[1])
    cache = fetch_all_days(force=force, max_days=max_days)
    print(f"\nSENSEX Option OI cache: {len(cache)} days")
    if cache:
        last_d = max(cache.keys())
        day_data = cache[last_d]
        strikes = sorted(day_data.keys())
        print(f"Last day: {last_d}, strikes: {len(strikes)}, "
              f"range: {strikes[0]}-{strikes[-1]}")
        mid = strikes[len(strikes) // 2]
        ce_df = day_data[mid]['CE']
        print(f"Sample strike {mid} CE: {len(ce_df)} candles")
