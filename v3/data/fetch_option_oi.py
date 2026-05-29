"""
v3/data/fetch_option_oi.py
===========================
Fetch 1m candles for individual BankNifty option strikes (CE + PE)
for each historical trading day in the 1m candle cache.

For each day:
  1. Get day's open price from futures candle cache
  2. Identify strikes within ±STRIKE_BAND of open (100-pt intervals)
  3. Fetch 1m candles for each strike CE + PE
  4. Extract OI column per 1m candle

Cache: v3/cache/option_oi_1m_BANKNIFTY.pkl
Format: {date_str: {strike: {CE: DataFrame[ts, oi], PE: DataFrame[ts, oi]}}}

Requires: valid token.env (GROWW_API_KEY + GROWW_TOTP_SECRET)
Run this ONCE from terminal: python v3/data/fetch_option_oi.py
Each day: ~40 strikes × 2 sides = ~80 API calls × ~0.3s = ~25s per day
22 days = ~9 minutes total
"""
import os, sys, time, pickle, pyotp, logging
from datetime import date, timedelta
from pathlib import Path

# NSE exchange holidays — markets closed these days.
# If a Thursday falls here, BankNifty weekly expiry shifts to the preceding Wednesday.
_NSE_HOLIDAYS: frozenset = frozenset([
    # 2024
    date(2024, 1, 22),   # Ram Lalla Pran Pratishtha
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 11),   # Shri Ram Navami (Thursday → expiry Apr 10)
    date(2024, 4, 14),   # Dr. Ambedkar Jayanti / Visu
    date(2024, 4, 17),   # Shri Mahavir Jayanti
    date(2024, 5, 23),   # Buddha Purnima (Thursday → expiry May 22)
    date(2024, 6, 17),   # Bakri Id
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day (Thursday → expiry Aug 14)
    date(2024, 10, 2),   # Gandhi Jayanti (Wednesday)
    date(2024, 10, 12),  # Dussehra
    date(2024, 11, 1),   # Diwali Laxmi Puja (Friday)
    date(2024, 11, 15),  # Gurunanak Jayanti (Friday)
    date(2024, 11, 20),  # Maharashtra Vidhan Sabha election
    date(2024, 12, 25),  # Christmas (Wednesday)
    # 2025
    date(2025, 2, 26),   # Mahashivratri (Wednesday)
    date(2025, 3, 14),   # Holi (Friday)
    date(2025, 4, 10),   # Shri Ram Navami (Thursday → expiry Apr 9)
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti (Monday)
    date(2025, 4, 18),   # Good Friday (Friday)
    date(2025, 5, 1),    # Maharashtra Day (Thursday → expiry Apr 30)
    date(2025, 8, 15),   # Independence Day (Friday)
    date(2025, 8, 27),   # Ganesh Chaturthi (Wednesday)
    date(2025, 10, 2),   # Gandhi Jayanti (Thursday → expiry Oct 1)
    date(2025, 10, 24),  # Dussehra (Friday)
    date(2025, 11, 5),   # Diwali Laxmi Puja (Wednesday)
    date(2025, 11, 15),  # Gurunanak Jayanti (Saturday)
    date(2025, 12, 25),  # Christmas (Thursday → expiry Dec 24)
    # 2026
    date(2026, 1, 26),   # Republic Day (Monday)
    date(2026, 2, 19),   # Chhatrapati Shivaji Maharaj Jayanti (Thursday → expiry Feb 18)
    date(2026, 3, 3),    # Mahashivratri (Tuesday)
    date(2026, 3, 17),   # Holi (Tuesday)
    date(2026, 4, 3),    # Good Friday (Friday)
    date(2026, 4, 6),    # Ram Navami (Monday)
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti (Tuesday)
    # Note: Buddha Purnima 2026 falls in May, NOT April 30 — Apr 30 is a trading day
])

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from v3.data.oi_cache_merge import merge_day_oi

CACHE_FILE  = ROOT / 'v3' / 'cache' / 'option_oi_1m_BANKNIFTY.pkl'
CANDLE_FILE = ROOT / 'v3' / 'cache' / 'candles_1m_BANKNIFTY.pkl'
STRIKE_BAND = 1500   # ± from open price
STRIKE_STEP = 100    # BankNifty 100-pt strikes

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fetch_option_oi')


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


def _get_monthly_expiry(g, trade_date: date) -> str:
    """
    Get the BankNifty monthly contract expiry on or after trade_date.

    Uses Groww's get_expiries() which returns the monthly expiry date.
    This is the contract that Groww stores historical intraday candle data for.
    Weekly contracts are NOT stored historically — only the monthly series.

    get_expiries() correctly returns expired months' dates even when called
    from a future date (e.g. calling from May 2026 still returns Mar 2026 = '2026-03-30').
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
                exchange='NSE', underlying_symbol='BANKNIFTY', year=y, month=m
            )
            for exp in sorted(result.get('expiries', [])):
                exp_d = date.fromisoformat(exp)
                if exp_d >= trade_date:
                    log.debug(f"Expiry for {trade_date}: {exp}")
                    return exp
        except Exception as e:
            log.warning(f"get_expiries {y}-{m:02d}: {e}")
        time.sleep(0.3)
    raise RuntimeError(f"No monthly expiry found for {trade_date}")


_SYMBOL_FORMAT: str = None   # detected at runtime


def _candidate_symbols(expiry_str: str, strike: int, side: str) -> list[str]:
    """All plausible Groww option symbol formats to probe."""
    d = date.fromisoformat(expiry_str)
    day_bare   = str(d.day)                 # "30"  (no padding)
    day_padded = d.strftime('%d')           # "30"  (zero-padded)
    mon_title  = d.strftime('%b')           # "Mar"
    mon_upper  = d.strftime('%b').upper()   # "MAR"
    yr_short   = d.strftime('%y')           # "26"
    yr_long    = str(d.year)               # "2026"
    return [
        f"NSE-BANKNIFTY-{day_bare}{mon_title}{yr_short}-{strike}-{side}",    # 30Mar26
        f"NSE-BANKNIFTY-{day_padded}{mon_title}{yr_short}-{strike}-{side}",  # 30Mar26 (padded)
        f"NSE-BANKNIFTY-{day_bare}{mon_upper}{yr_long}-{strike}-{side}",     # 30MAR2026
        f"NSE-BANKNIFTY-{day_padded}{mon_upper}{yr_long}-{strike}-{side}",   # 30MAR2026 (padded)
        f"NSE-BANKNIFTY-{day_bare}{mon_upper}{yr_short}-{strike}-{side}",    # 30MAR26
        f"NSE-BANKNIFTY-{day_padded}{mon_upper}{yr_short}-{strike}-{side}",  # 30MAR26 (padded)
    ]


def _option_symbol(expiry_str: str, strike: int, side: str) -> str:
    """Return option symbol in the detected/default Groww format."""
    global _SYMBOL_FORMAT
    d = date.fromisoformat(expiry_str)
    if _SYMBOL_FORMAT is None:
        _SYMBOL_FORMAT = "{day}{mon_title}{yr_short}"   # default; overridden after probe
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
    return f"NSE-BANKNIFTY-{fmt}-{strike}-{side}"


def _probe_symbol_format(g, expiry_str: str, strike: int,
                          trade_date: date) -> str | None:
    """
    Try each candidate symbol format until one returns candles.
    Returns the format template string, or None if all fail.
    """
    global _SYMBOL_FORMAT
    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T09:30:00"   # just 15 minutes to probe quickly

    for sym in _candidate_symbols(expiry_str, strike, 'CE'):
        try:
            r = g.get_historical_candles(
                exchange='NSE', segment='FNO', groww_symbol=sym,
                start_time=start, end_time=end,
                candle_interval=g.CANDLE_INTERVAL_MIN_1,
            )
            if r.get('candles'):
                # Extract format template from the working symbol
                # Symbol looks like NSE-BANKNIFTY-<DATE_PART>-<STRIKE>-CE
                date_part = sym.split('-')[2]   # e.g. "30Mar26"
                d = date.fromisoformat(expiry_str)
                # Figure out which template produced this date_part
                templates = [
                    ("{day}{mon_title}{yr_short}", f"{d.day}{d.strftime('%b')}{d.strftime('%y')}"),
                    ("{day_pad}{mon_title}{yr_short}", f"{d.strftime('%d')}{d.strftime('%b')}{d.strftime('%y')}"),
                    ("{day}{mon_upper}{yr_long}",  f"{d.day}{d.strftime('%b').upper()}{d.year}"),
                    ("{day_pad}{mon_upper}{yr_long}", f"{d.strftime('%d')}{d.strftime('%b').upper()}{d.year}"),
                    ("{day}{mon_upper}{yr_short}", f"{d.day}{d.strftime('%b').upper()}{d.strftime('%y')}"),
                    ("{day_pad}{mon_upper}{yr_short}", f"{d.strftime('%d')}{d.strftime('%b').upper()}{d.strftime('%y')}"),
                ]
                for tmpl, val in templates:
                    if val == date_part:
                        _SYMBOL_FORMAT = tmpl
                        log.info(f"Symbol format detected: {sym}  (template: {tmpl})")
                        return tmpl
        except Exception as e:
            log.debug(f"Probe {sym}: {e}")
        time.sleep(0.1)

    log.error("Could not detect symbol format — all candidates failed")
    return None


def _fetch_option_oi_day(g, trade_date: date, open_price: float) -> dict:
    """
    Fetch 1m OI for all strikes ±STRIKE_BAND of open_price.
    Returns {strike: {'CE': DataFrame[ts,oi], 'PE': DataFrame[ts,oi]}}
    """
    global _SYMBOL_FORMAT
    expiry = _get_monthly_expiry(g, trade_date)
    log.info(f"  {trade_date}: open={open_price:.0f}, expiry={expiry}")

    # Probe symbol format once — on first day
    if _SYMBOL_FORMAT is None:
        base_probe = round(open_price / STRIKE_STEP) * STRIKE_STEP
        fmt = _probe_symbol_format(g, expiry, base_probe, trade_date)
        if fmt is None:
            log.error(f"Aborting day {trade_date} — symbol format unknown")
            return {}
        time.sleep(0.3)

    # Round open to nearest 100
    base_strike = round(open_price / STRIKE_STEP) * STRIKE_STEP
    strikes = range(base_strike - STRIKE_BAND, base_strike + STRIKE_BAND + 1, STRIKE_STEP)

    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T15:30:00"

    day_result = {}
    fetched = 0
    errors  = 0

    for strike in strikes:
        entry = {}
        for side in ['CE', 'PE']:
            sym = _option_symbol(expiry, strike, side)
            try:
                r = g.get_historical_candles(
                    exchange='NSE', segment='FNO', groww_symbol=sym,
                    start_time=start, end_time=end,
                    candle_interval=g.CANDLE_INTERVAL_MIN_1,
                )
                candles_raw = r.get('candles', [])
                if not candles_raw:
                    entry[side] = pd.DataFrame(columns=['ts','oi'])
                    continue
                df = pd.DataFrame(candles_raw,
                                  columns=['ts','open','high','low','close','volume','oi'])
                df['ts'] = pd.to_datetime(df['ts'])
                df['oi'] = pd.to_numeric(df['oi'], errors='coerce').ffill().fillna(0)
                entry[side] = df[['ts', 'oi']].copy()
                fetched += 1
            except Exception as e:
                if errors < 3:   # show first 3 errors in full to diagnose format
                    log.warning(f"    FETCH ERROR {sym}: {e}")
                else:
                    log.debug(f"    {sym}: {e}")
                entry[side] = pd.DataFrame(columns=['ts', 'oi'])
                errors += 1
            time.sleep(0.15)

        day_result[strike] = entry

    log.info(f"  {trade_date}: {fetched} option series fetched, {errors} errors")
    return day_result


def fetch_all_days(force: bool = False, max_days: int = 0):
    """Main entry: fetch all days in 1m candle cache."""
    global _SYMBOL_FORMAT
    _SYMBOL_FORMAT = None   # reset so probe fires on first day

    # Load futures candle cache for trade dates + open prices
    with open(CANDLE_FILE, 'rb') as f:
        candles = pickle.load(f)

    day_opens = {}
    for td in sorted(candles['date'].unique()):
        day_df = candles[candles['date'] == td].sort_values('ts')
        if not day_df.empty:
            day_opens[td] = float(day_df['open'].iloc[0])

    log.info(f"Trade days in 1m cache: {len(day_opens)}")

    # Load existing option OI cache
    if CACHE_FILE.exists() and not force:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
        log.info(f"Existing option OI cache: {len(cache)} days")
    else:
        cache = {}

    already_done = set(str(d) for d in cache.keys())
    g = _get_groww()
    fetched_this_run = 0

    for td, open_px in sorted(day_opens.items()):
        if max_days > 0 and fetched_this_run >= max_days:
            log.info(f"max_days={max_days} reached, stopping")
            break
        if str(td) in already_done:
            log.info(f"Skip {td} (cached)")
            continue

        try:
            day_data = _fetch_option_oi_day(g, td, open_px)
            # Merge, never overwrite — preserve real OI captured live (active
            # contracts return no OI in historical candles).
            cache[str(td)] = merge_day_oi(cache.get(str(td)), day_data)
            # Save incrementally
            with open(CACHE_FILE, 'wb') as f:
                pickle.dump(cache, f)
            log.info(f"Saved {td}: {len(cache[str(td)])} strikes")
            fetched_this_run += 1
        except Exception as e:
            log.error(f"FAILED {td}: {e}")

        time.sleep(1.0)

    log.info(f"Done. Cache: {len(cache)} days at {CACHE_FILE}")
    return cache


def compute_intraday_velocity(day_cache: dict, spot: float, band: int = 1000,
                               window_minutes: int = 5) -> dict:
    """
    Compute OI velocity from 1m option candles for a single day.
    velocity = (OI_now - OI_{window_minutes_ago}) / window_minutes per strike.

    Use latest candles as reference.
    Returns same format as options_chain.compute_oi_velocity:
      {strike: {ce_velocity, pe_velocity, net_velocity, ce_oi, pe_oi}}
    """
    result = {}
    for strike, sides in day_cache.items():
        if abs(strike - spot) > band:
            continue
        try:
            ce_df = sides.get('CE', pd.DataFrame())
            pe_df = sides.get('PE', pd.DataFrame())
            if ce_df.empty or pe_df.empty:
                continue

            ce_oi_now  = float(ce_df['oi'].iloc[-1])
            pe_oi_now  = float(pe_df['oi'].iloc[-1])
            ce_oi_prev = float(ce_df['oi'].iloc[-min(window_minutes+1, len(ce_df))])
            pe_oi_prev = float(pe_df['oi'].iloc[-min(window_minutes+1, len(pe_df))])

            ce_vel = (ce_oi_now - ce_oi_prev) / window_minutes
            pe_vel = (pe_oi_now - pe_oi_prev) / window_minutes
            net    = pe_vel - ce_vel

            result[strike] = {
                'ce_oi': ce_oi_now, 'pe_oi': pe_oi_now,
                'ce_velocity': round(ce_vel, 2),
                'pe_velocity': round(pe_vel, 2),
                'net_velocity': round(net, 2),
            }
        except Exception:
            continue
    return result


def compute_eod_walls(day_cache: dict, spot: float, band: int = 2000) -> dict:
    """
    Derive call_wall / put_wall from intraday option OI (using last 1m snapshot).
    More accurate than bhavcopy EOD because it's the same day's intraday OI.
    """
    strikes_oi = {}
    for strike, sides in day_cache.items():
        if abs(strike - spot) > band:
            continue
        try:
            ce_oi = float(sides['CE']['oi'].iloc[-1]) if not sides['CE'].empty else 0
            pe_oi = float(sides['PE']['oi'].iloc[-1]) if not sides['PE'].empty else 0
            strikes_oi[strike] = {'ce_oi': ce_oi, 'pe_oi': pe_oi}
        except Exception:
            continue

    if not strikes_oi:
        return {}

    total_ce = sum(v['ce_oi'] for v in strikes_oi.values())
    total_pe = sum(v['pe_oi'] for v in strikes_oi.values())
    pcr_live = total_pe / total_ce if total_ce > 0 else 1.0

    calls_above = {s: v for s, v in strikes_oi.items() if s > spot}
    puts_below  = {s: v for s, v in strikes_oi.items() if s < spot}

    call_wall = max(calls_above, key=lambda s: calls_above[s]['ce_oi']) if calls_above else None
    put_wall  = max(puts_below,  key=lambda s: puts_below[s]['pe_oi'])  if puts_below  else None

    return {
        'call_wall': call_wall, 'put_wall': put_wall,
        'pcr_live': round(pcr_live, 3), 'ltp': spot,
    }


if __name__ == '__main__':
    import sys
    force = '--force' in sys.argv
    max_days = 0
    for arg in sys.argv[1:]:
        if arg.startswith('--days='):
            max_days = int(arg.split('=')[1])
    cache = fetch_all_days(force=force, max_days=max_days)
    print(f"\nOption OI cache: {len(cache)} days")
    # Quick sanity check on latest day
    if cache:
        last_d = max(cache.keys())
        day_data = cache[last_d]
        strikes = sorted(day_data.keys())
        print(f"Last day: {last_d}, strikes: {len(strikes)}, "
              f"range: {strikes[0]}-{strikes[-1]}")
        # Show a sample strike
        mid = strikes[len(strikes)//2]
        ce_df = day_data[mid]['CE']
        print(f"Sample strike {mid} CE: {len(ce_df)} candles, "
              f"OI range {ce_df['oi'].min():.0f}–{ce_df['oi'].max():.0f}")
