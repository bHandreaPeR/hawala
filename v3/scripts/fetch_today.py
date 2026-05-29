"""
v3/scripts/fetch_today.py
==========================
One-shot script: fetch ALL data for TODAY and show bar-by-bar signal evaluation.

Usage:
    cd "Hawala v2/Hawala v2"
    python3 v3/scripts/fetch_today.py [--date YYYY-MM-DD]

What it does:
  1. Authenticates with Groww (reads token.env)
  2. Force re-fetches today's NIFTY option OI 1m candles for all strikes ±1000
  3. Fetches today's NSE bhavcopy → updates PCR cache
  4. Runs a full bar-by-bar signal evaluation (no lookahead)
  5. Prints every bar where a signal fired, and a final summary

Exits with code 1 on any hard failure (auth error, network error).
"""
import sys, pickle, logging, argparse, time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('fetch_today')

CANDLE_CACHE   = ROOT / 'v3' / 'cache' / 'candles_1m_NIFTY.pkl'
OI_CACHE       = ROOT / 'v3' / 'cache' / 'option_oi_1m_NIFTY.pkl'
PCR_CACHE      = ROOT / 'v3' / 'cache' / 'pcr_daily.csv'
BHAV_CACHE     = ROOT / 'v3' / 'cache' / 'bhavcopy_NIFTY_all.pkl'

SCORE_THRESHOLD = 0.35
VELOCITY_WINDOW = 10     # bars for rolling OI velocity

# ── Step 1: Auth ──────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    env = {}
    token_path = ROOT / 'token.env'
    if not token_path.exists():
        raise FileNotFoundError(
            f"token.env not found at {token_path}. "
            "Create it with GROWW_API_KEY=... and GROWW_TOTP_SECRET=..."
        )
    with open(token_path) as f:
        for line in f:
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k.strip()] = v.strip()
    import pyotp
    totp  = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
    return GrowwAPI(token=token)


# ── Step 2: Re-fetch today's option OI ────────────────────────────────────────
def fetch_option_oi_today(g, trade_date: date) -> dict:
    """
    Import the existing per-day fetcher from fetch_option_oi_NIFTY.py
    and run it for trade_date. Returns {strike: {CE: df, PE: df}}.
    """
    from v3.data.fetch_option_oi_NIFTY import _fetch_option_oi_day

    # We need the open price from the futures candle cache to centre strikes.
    if not CANDLE_CACHE.exists():
        raise FileNotFoundError(
            f"Futures candle cache not found: {CANDLE_CACHE}. "
            "Run v3/data/fetch_1m_NIFTY.py first."
        )
    with open(CANDLE_CACHE, 'rb') as fh:
        candles = pickle.load(fh)

    day_candles = candles[candles['date'].astype(str) == str(trade_date)]
    if day_candles.empty:
        raise ValueError(
            f"No futures candles found for {trade_date} in {CANDLE_CACHE}. "
            "Run v3/data/fetch_1m_NIFTY.py to fetch today's candles first."
        )

    open_price = float(day_candles.iloc[0]['open'])
    log.info("Futures open price for %s: %.0f", trade_date, open_price)

    log.info("Fetching option OI for %s (open=%.0f) — ~80 API calls, ~30s…",
             trade_date, open_price)
    day_oi = _fetch_option_oi_day(g, trade_date, open_price)

    # Count non-empty strikes
    non_empty = sum(
        1 for sides in day_oi.values()
        if not sides.get('CE', pd.DataFrame()).empty
        or not sides.get('PE', pd.DataFrame()).empty
    )
    if non_empty == 0:
        raise RuntimeError(
            f"All {len(day_oi)} strikes returned empty option data for {trade_date}. "
            "Possible causes: wrong expiry, Groww returned no data for these contracts, "
            "or the session date is a holiday."
        )
    log.info(
        "Option OI fetched: %d strikes, %d non-empty",
        len(day_oi), non_empty
    )

    # Merge into the OI cache, never overwrite. get_historical_candles omits OI
    # for a still-active contract (oi=0), so a blind same-day overwrite would
    # wipe the real OI the option_flow_daemon/runners captured live from the
    # option chain. merge_day_oi keeps real OI and only fills gaps.
    from v3.data.oi_cache_merge import merge_day_oi
    oi_cache: dict = {}
    if OI_CACHE.exists():
        with open(OI_CACHE, 'rb') as fh:
            oi_cache = pickle.load(fh)
    merged = merge_day_oi(oi_cache.get(str(trade_date)), day_oi)
    oi_cache[str(trade_date)] = merged
    with open(OI_CACHE, 'wb') as fh:
        pickle.dump(oi_cache, fh)
    log.info("Option OI cache updated (merged) → %s", OI_CACHE)
    return merged


# ── Step 3: Fetch bhavcopy PCR for today ─────────────────────────────────────
def fetch_bhavcopy_today(trade_date: date) -> float | None:
    """
    Fetch NSE bhavcopy for trade_date, update both the bhavcopy cache and
    pcr_daily.csv.  Returns today's PCR, or None if not yet available (pre-close).
    """
    from v3.data.fetch_bhavcopy_nifty import _fetch_nifty_day
    import requests

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        'Accept': '*/*',
        'Referer': 'https://www.nseindia.com',
    })

    log.info("Fetching bhavcopy for %s from NSE…", trade_date)
    try:
        df = _fetch_nifty_day(trade_date, session)
    except Exception as e:
        log.warning(
            "bhavcopy fetch failed for %s: %s — PCR will be null for today",
            trade_date, e
        )
        return None

    if df.empty:
        log.warning(
            "bhavcopy returned empty for %s — NSE may not have published yet "
            "(bhavcopy is typically available after 15:30 IST)",
            trade_date
        )
        return None

    # Compute PCR from this day's bhavcopy
    total_ce_oi = float(df['ce_oi'].sum())
    total_pe_oi = float(df['pe_oi'].sum())
    pcr = round(total_pe_oi / total_ce_oi, 4) if total_ce_oi > 0 else 0.0
    log.info("Today's PCR: %.4f (CE_OI=%.0f, PE_OI=%.0f)", pcr, total_ce_oi, total_pe_oi)

    # Update bhavcopy cache
    bhav_cache: dict = {}
    if BHAV_CACHE.exists():
        with open(BHAV_CACHE, 'rb') as fh:
            bhav_cache = pickle.load(fh)
    bhav_cache[str(trade_date)] = df
    with open(BHAV_CACHE, 'wb') as fh:
        pickle.dump(bhav_cache, fh)
    log.info("Bhavcopy cache updated → %s", BHAV_CACHE)

    # Update pcr_daily.csv
    pcr_df = pd.DataFrame()
    if PCR_CACHE.exists():
        pcr_df = pd.read_csv(PCR_CACHE, parse_dates=['date'])

    today_str = str(trade_date)
    # Remove existing entry for today if any
    if not pcr_df.empty and 'date' in pcr_df.columns:
        pcr_df = pcr_df[pcr_df['date'].astype(str).str[:10] != today_str]
    new_row = pd.DataFrame([{'date': today_str, 'pcr': pcr}])
    pcr_df  = pd.concat([pcr_df, new_row], ignore_index=True)
    pcr_df['date'] = pcr_df['date'].astype(str).str[:10]
    pcr_df  = pcr_df.sort_values('date').reset_index(drop=True)
    pcr_df['pcr_5d_ma'] = pcr_df['pcr'].rolling(5, min_periods=1).mean()
    pcr_df.to_csv(PCR_CACHE, index=False)
    log.info("PCR cache updated → %s (%.4f)", PCR_CACHE, pcr)
    return pcr


# ── Step 4: Bar-by-bar signal evaluation ─────────────────────────────────────
def _build_vel_cache(day_oi: dict) -> dict:
    """Pre-build {strike: {CE: np.array, PE: np.array}} for fast per-bar velocity."""
    cache: dict = {}
    for strike, sides in day_oi.items():
        entry = {}
        for side in ('CE', 'PE'):
            df = sides.get(side, pd.DataFrame())
            if not df.empty and 'oi' in df.columns:
                entry[side] = df['oi'].to_numpy(dtype=float, na_value=0.0)
            else:
                entry[side] = np.empty(0)
        cache[strike] = entry
    return cache


def _compute_velocity(vel_cache: dict, spot: float, bar_idx: int,
                       window: int = VELOCITY_WINDOW, band_pct: float = 0.05) -> dict:
    """Compute rolling OI velocity at bar_idx using pre-built numpy arrays."""
    band     = spot * band_pct
    from_bar = max(0, bar_idx - window)
    result   = {}
    for strike, sides in vel_cache.items():
        if abs(strike - spot) > band:
            continue
        ce_arr = sides.get('CE', np.empty(0))
        pe_arr = sides.get('PE', np.empty(0))
        if len(ce_arr) == 0 or len(pe_arr) == 0:
            continue
        ce_end   = min(bar_idx, len(ce_arr) - 1)
        pe_end   = min(bar_idx, len(pe_arr) - 1)
        ce_start = min(from_bar, ce_end)
        pe_start = min(from_bar, pe_end)
        n = bar_idx - from_bar
        if n == 0:
            continue
        ce_vel = (float(ce_arr[ce_end]) - float(ce_arr[ce_start])) / n
        pe_vel = (float(pe_arr[pe_end]) - float(pe_arr[pe_start])) / n
        result[strike] = {
            'ce_oi':        float(ce_arr[ce_end]),
            'pe_oi':        float(pe_arr[pe_end]),
            'ce_velocity':  round(ce_vel, 2),
            'pe_velocity':  round(pe_vel, 2),
            'net_velocity': round(pe_vel - ce_vel, 2),
        }
    return result


def _get_dte(trade_date: date) -> int:
    """Days to next weekly Nifty expiry (Tuesday) from trade_date."""
    # Find next Tuesday on or after trade_date
    days_ahead = (1 - trade_date.weekday()) % 7   # weekday 1 = Tuesday
    next_tue   = trade_date + timedelta(days=days_ahead)
    return (next_tue - trade_date).days


def _get_walls_from_prev_day(bhav_cache: dict, trade_date: date, spot: float,
                              band: int = 1500) -> dict:
    """Get call/put walls from prev-day bhavcopy (lag-1, no lookahead)."""
    prev_dates = sorted(
        d for d in bhav_cache if d < str(trade_date)
    )
    if not prev_dates:
        return {}
    prev_df = bhav_cache[prev_dates[-1]]
    if prev_df.empty:
        return {}
    in_band = prev_df[
        (prev_df['strike'] >= spot - band) & (prev_df['strike'] <= spot + band)
    ]
    if in_band.empty:
        return {}
    call_wall = int(in_band.loc[in_band['ce_oi'].idxmax(), 'strike'])
    put_wall  = int(in_band.loc[in_band['pe_oi'].idxmax(), 'strike'])
    pcr_live  = (
        float(in_band['pe_oi'].sum()) / float(in_band['ce_oi'].sum())
        if in_band['ce_oi'].sum() > 0 else 1.0
    )
    return {
        'call_wall': call_wall,
        'put_wall':  put_wall,
        'pcr_live':  round(pcr_live, 4),
    }


def run_signal_eval(trade_date: date, day_oi: dict, today_pcr: float | None) -> None:
    """
    Bar-by-bar signal evaluation for trade_date.
    Prints every bar where the engine fires (|score| >= threshold) and a summary.
    """
    from v3.signals.engine import compute_signal_state

    # ── Load futures candles ──────────────────────────────────────────────────
    with open(CANDLE_CACHE, 'rb') as fh:
        candles = pickle.load(fh)
    day_df = candles[candles['date'].astype(str) == str(trade_date)].copy()
    day_df = day_df.sort_values('ts').reset_index(drop=True)
    if day_df.empty:
        raise ValueError(f"No futures candles for {trade_date}")
    log.info("Loaded %d futures candles for %s", len(day_df), trade_date)

    # ── PCR setup ─────────────────────────────────────────────────────────────
    pcr_val = today_pcr
    pcr_ma  = None
    if PCR_CACHE.exists():
        pcr_df = pd.read_csv(PCR_CACHE)
        pcr_df['date_str'] = pcr_df['date'].astype(str).str[:10]

        # First: check if today is already in the cache (e.g. manually added or
        # bhavcopy was fetched earlier in this run).
        today_row = pcr_df[pcr_df['date_str'] == str(trade_date)]
        if pcr_val is None and not today_row.empty:
            pcr_val = float(today_row.iloc[0]['pcr'])
            pcr_ma  = today_row.iloc[0].get('pcr_5d_ma', None)
            pcr_ma  = float(pcr_ma) if pd.notna(pcr_ma) else pcr_val
            log.info("Using today's PCR from cache: %.4f (5d_ma=%.4f)", pcr_val, pcr_ma)

        # Fallback: lag-1 (yesterday)
        if pcr_val is None:
            prior = pcr_df[pcr_df['date_str'] < str(trade_date)].tail(1)
            if not prior.empty:
                pcr_val = float(prior.iloc[0]['pcr'])
                pcr_ma  = prior.iloc[0].get('pcr_5d_ma', None)
                pcr_ma  = float(pcr_ma) if pd.notna(pcr_ma) else pcr_val
                log.info(
                    "Using lag-1 PCR (today not in cache): %.4f (5d_ma=%.4f)",
                    pcr_val, pcr_ma
                )
        elif pcr_ma is None:
            # pcr_val came from today_pcr arg, get 5d_ma from cache
            prior = pcr_df[pcr_df['date_str'] < str(trade_date)].tail(5)
            if not prior.empty:
                pcr_ma = float(prior['pcr'].mean())

    if pcr_val is None:
        pcr_val = 1.0
        log.warning("No PCR available — using 1.0 (neutral)")

    # ── Bhavcopy walls (prev-day) ─────────────────────────────────────────────
    bhav_cache: dict = {}
    if BHAV_CACHE.exists():
        with open(BHAV_CACHE, 'rb') as fh:
            bhav_cache = pickle.load(fh)

    open_price = float(day_df.iloc[0]['close'])
    walls = _get_walls_from_prev_day(bhav_cache, trade_date, open_price)
    log.info(
        "Walls (prev-day bhavcopy): call_wall=%s put_wall=%s pcr_live=%s",
        walls.get('call_wall'), walls.get('put_wall'), walls.get('pcr_live')
    )

    # ── DTE ──────────────────────────────────────────────────────────────────
    dte = _get_dte(trade_date)
    log.info("DTE for %s: %d", trade_date, dte)

    # ── Build velocity cache from option OI ──────────────────────────────────
    has_oi = bool(day_oi)
    vel_cache = _build_vel_cache(day_oi) if has_oi else {}
    if not has_oi:
        log.warning(
            "No option OI data for %s — OI velocity and strike defense will be 0 "
            "(run this script with Groww auth to populate option OI)",
            trade_date
        )
    else:
        non_empty = sum(
            1 for s in day_oi.values()
            if not s.get('CE', pd.DataFrame()).empty
            or not s.get('PE', pd.DataFrame()).empty
        )
        log.info("Velocity cache built from %d strikes (%d non-empty)", len(day_oi), non_empty)

    # ── Bar-by-bar evaluation ─────────────────────────────────────────────────
    print("\n" + "═" * 80)
    print(f"  BAR-BY-BAR SIGNAL EVALUATION  |  {trade_date}  |  NIFTY")
    print("═" * 80)
    print(f"  PCR = {pcr_val:.4f}  |  DTE = {dte}  |  OI data = {'YES' if has_oi else 'NO'}")
    print(f"  Threshold = {SCORE_THRESHOLD}  |  Walls = {walls or 'none'}")
    print("─" * 80)
    print(f"  {'TIME':<8} {'CLOSE':>8} {'SCORE':>7} {'DIR':<8} {'SIGNALS'}")
    print("─" * 80)

    signals_fired = []          # only non-zero direction entries
    prev_nonzero_dir = 0        # last direction that was actually LONG or SHORT
    last_printed_dir = 0        # what we last printed (to suppress repeats)

    for bar_idx, row in day_df.iterrows():
        df_so_far = day_df.iloc[: bar_idx + 1]
        futures_ltp = float(row['close'])
        ts = row['ts']

        # Spot: approximate as futures.
        # No separate spot candle cache — futures LTP is a ~0.05% proxy for spot.
        spot_ltp = futures_ltp

        # Velocity at this bar (from option OI)
        velocity_data = (
            _compute_velocity(vel_cache, futures_ltp, bar_idx)
            if has_oi else {}
        )

        try:
            state = compute_signal_state(
                df_1m           = df_so_far,
                futures_ltp     = futures_ltp,
                spot_ltp        = spot_ltp,
                days_to_expiry  = dte,
                pcr             = pcr_val,
                pcr_5d_ma       = pcr_ma,
                velocity_data   = velocity_data,
                walls           = walls,
                fii_fut_level   = 0,   # FII cash data not available for today
                fii_cash_lag1   = 0,
                timestamp       = ts,
            )
        except Exception as e:
            log.warning("Signal compute error at bar %s: %s", ts, e)
            continue

        score     = round(state.score, 3)
        direction = state.direction  # 1, -1, or 0 from SignalSmoother

        # Only care about bars that cross threshold AND have a confirmed direction.
        # Suppress NEUTRAL (direction=0) bars — those are just the smoother
        # waiting for 2 consecutive same-direction bars. They are noise here.
        fired = (abs(score) >= SCORE_THRESHOLD) and (direction != 0)

        if fired:
            # Track actual direction (non-zero), ignore NEUTRAL bounces
            if direction != 0:
                prev_nonzero_dir = direction

            dir_str = "LONG  ↑" if direction == 1 else "SHORT ↓"
            sigs = []
            if state.oi_quadrant    != 0: sigs.append(f"OI_Q={'+' if state.oi_quadrant>0 else '-'}{abs(state.oi_quadrant)}")
            if state.futures_basis  != 0: sigs.append(f"BASIS={'+' if state.futures_basis>0 else '-'}{abs(state.futures_basis)}")
            if state.pcr            != 0: sigs.append(f"PCR={'+' if state.pcr>0 else '-'}{abs(state.pcr)}")
            if state.oi_velocity    != 0: sigs.append(f"VEL={'+' if state.oi_velocity>0 else '-'}{abs(state.oi_velocity)}")
            if state.strike_defense != 0: sigs.append(f"DEF={'+' if state.strike_defense>0 else '-'}{abs(state.strike_defense)}")

            # Print on: first signal, or direction change (SHORT→LONG or vice versa)
            is_new = (direction != last_printed_dir)
            if is_new:
                marker = "◀ NEW" if last_printed_dir != 0 else "◀ FIRST"
                print(f"  {ts.strftime('%H:%M'):<8} {futures_ltp:>8.1f} {score:>+7.3f}  {dir_str}  {', '.join(sigs)}  {marker}")
                signals_fired.append({
                    'ts': ts, 'ltp': futures_ltp, 'score': score,
                    'direction': direction, 'signals': sigs
                })
                last_printed_dir = direction

    print("─" * 80)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  SUMMARY — {len(signals_fired)} signal event(s) today\n")
    if not signals_fired:
        print("  No signals crossed threshold. Market was indecisive or data incomplete.")
        print(f"  (OI data: {'populated' if has_oi else 'MISSING — run with Groww auth to get option OI velocity'})")
    else:
        longs  = [s for s in signals_fired if s['direction'] ==  1]
        shorts = [s for s in signals_fired if s['direction'] == -1]
        print(f"  LONG  signals: {len(longs)}")
        print(f"  SHORT signals: {len(shorts)}")
        if longs:
            first = longs[0]
            print(f"  First LONG:  {first['ts'].strftime('%H:%M')}  ltp={first['ltp']:.1f}  score={first['score']:+.3f}")
        if shorts:
            first = shorts[0]
            print(f"  First SHORT: {first['ts'].strftime('%H:%M')}  ltp={first['ltp']:.1f}  score={first['score']:+.3f}")

    print("\n" + "═" * 80 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Fetch today\'s data and run bar-by-bar signal evaluation.'
    )
    parser.add_argument(
        '--date', default=str(date.today()),
        help='Trading date to process (YYYY-MM-DD). Defaults to today.'
    )
    parser.add_argument(
        '--skip-option-oi', action='store_true',
        help='Skip option OI fetch (use existing cache). Useful for offline eval.'
    )
    parser.add_argument(
        '--skip-bhavcopy', action='store_true',
        help='Skip bhavcopy fetch (use existing PCR cache).'
    )
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.date)
    log.info("Processing trade_date=%s", trade_date)

    # ── Step 1: Auth ──────────────────────────────────────────────────────────
    g = None
    if not args.skip_option_oi:
        log.info("Authenticating with Groww…")
        try:
            g = _get_groww()
            log.info("Groww auth OK")
        except Exception as e:
            log.error("Groww auth FAILED: %s", e)
            raise SystemExit(1) from e

    # ── Step 2: Option OI ─────────────────────────────────────────────────────
    day_oi: dict = {}
    if not args.skip_option_oi:
        try:
            day_oi = fetch_option_oi_today(g, trade_date)
        except RuntimeError as e:
            log.error("Option OI fetch failed: %s", e)
            raise SystemExit(1) from e
    else:
        log.info("--skip-option-oi: loading from cache")
        if OI_CACHE.exists():
            with open(OI_CACHE, 'rb') as fh:
                oi_cache = pickle.load(fh)
            day_oi = oi_cache.get(str(trade_date), {})
            non_empty = sum(
                1 for s in day_oi.values()
                if not s.get('CE', pd.DataFrame()).empty
            )
            log.info("Loaded from cache: %d strikes (%d non-empty)", len(day_oi), non_empty)

    # ── Step 3: Bhavcopy / PCR ────────────────────────────────────────────────
    today_pcr = None
    if not args.skip_bhavcopy:
        today_pcr = fetch_bhavcopy_today(trade_date)
    else:
        log.info("--skip-bhavcopy: using existing PCR cache")

    # ── Step 4: Signal evaluation ─────────────────────────────────────────────
    run_signal_eval(trade_date, day_oi, today_pcr)


if __name__ == '__main__':
    main()
