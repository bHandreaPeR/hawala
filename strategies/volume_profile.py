# ============================================================
# strategies/volume_profile.py — Volume Profile Retracement
# ============================================================
# Fades unbalanced thrusts out of the developing front-month
# Value Area back toward the Point of Control (POC).
#
# Two profiles are maintained side-by-side:
#   • profile_full — grows continuously across the front-month
#                    contract; resets only on roll.
#   • profile_sub  — a "regime-scoped" sub-profile that resets
#                    whenever a daily regime shift is detected
#                    (large gap + elevated volume + acceptance
#                    away from the prior POC).
#
# The trigger uses the SUB profile once it has matured
# (≥ VP_SUB_MIN_DAYS of bars), else falls back to FULL.
# This lets the system trade off the *currently operative*
# auction (e.g. SENSEX after the Apr 8 2026 gap-up) instead of
# being misled by a blended profile that spans two regimes.
# ============================================================

import numpy as np
import pandas as pd
from collections import deque
from datetime import time as dtime


# ── Profile primitives ────────────────────────────────────────────────────────

def _bar_distribute_volume(profile: dict, low: float, high: float,
                           volume: float, bin_pts: float) -> None:
    """
    Distribute one bar's traded volume evenly across the price bins it covers.
    """
    if volume is None or volume <= 0 or pd.isna(volume):
        return
    if not np.isfinite(low) or not np.isfinite(high) or high < low:
        return

    lo_bin = int(low // bin_pts) * bin_pts
    hi_bin = int(high // bin_pts) * bin_pts
    n      = int((hi_bin - lo_bin) // bin_pts) + 1
    if n < 1:
        n = 1
    vol_per = float(volume) / n

    b = lo_bin
    for _ in range(n):
        profile[b] = profile.get(b, 0.0) + vol_per
        b += bin_pts


def _value_area(profile: dict, bin_pts: float, va_pct: float = 0.70):
    """
    Canonical Steidlmayer / TradingView value-area construction:
    expand from POC by adding the larger of the next two-bin pair on
    either side until cumulative volume ≥ va_pct.
    """
    if not profile:
        return None, None, None, 0.0
    bins = sorted(profile.keys())
    vols = [profile[b] for b in bins]
    total = float(sum(vols))
    if total <= 0:
        return None, None, None, 0.0

    n       = len(bins)
    poc_idx = max(range(n), key=lambda i: vols[i])
    target  = total * va_pct
    cum     = vols[poc_idx]
    lo, hi  = poc_idx, poc_idx

    while cum < target and (lo > 0 or hi < n - 1):
        up1 = vols[hi + 1] if hi + 1 < n else 0.0
        up2 = vols[hi + 2] if hi + 2 < n else 0.0
        dn1 = vols[lo - 1] if lo - 1 >= 0 else 0.0
        dn2 = vols[lo - 2] if lo - 2 >= 0 else 0.0
        up_pair, dn_pair = up1 + up2, dn1 + dn2
        if up_pair == 0 and dn_pair == 0:
            break
        if up_pair >= dn_pair:
            for _ in range(2):
                if hi < n - 1:
                    hi += 1
                    cum += vols[hi]
        else:
            for _ in range(2):
                if lo > 0:
                    lo -= 1
                    cum += vols[lo]

    val = float(bins[lo])
    vah = float(bins[hi]) + bin_pts
    poc = float(bins[poc_idx]) + bin_pts / 2.0
    return vah, val, poc, total


# ── Regime-shift detection ────────────────────────────────────────────────────

def _build_daily_summary(data: pd.DataFrame) -> pd.DataFrame:
    """
    Per-trading-date OHLCV summary plus rolling ATR14 (daily range) and
    a 5-day prior volume baseline. Index = python date.

    When a `Contract` column is present, ATR14 / vol5avg / gap are scoped
    to the current front-month contract — values do not leak across roll
    boundaries (which would otherwise inflate the prior-volume baseline
    with the always-heavy first-2-days-of-contract volume).
    """
    g = data.groupby(data.index.date)
    daily = pd.DataFrame({
        'open':   g['Open'].first(),
        'high':   g['High'].max(),
        'low':    g['Low'].min(),
        'close':  g['Close'].last(),
        'volume': (g['Volume'].sum() if 'Volume' in data.columns
                   else g['Open'].count() * 0),
    })
    if 'Contract' in data.columns:
        daily['contract'] = g['Contract'].first()
    daily['range'] = daily['high'] - daily['low']

    if 'contract' in daily.columns:
        prev_close    = daily['close'].shift(1)
        prev_contract = daily['contract'].shift(1)
        same          = prev_contract == daily['contract']
        # Day is a contract roll if today's contract != yesterday's.
        # The very first row gets is_roll=False (no prior to compare).
        is_roll = ~same.fillna(True)
        if len(is_roll):
            is_roll.iloc[0] = False
        daily['is_roll'] = is_roll
        daily['gap']     = (daily['open'] - prev_close).where(same)

        daily['atr14'] = daily.groupby('contract')['range'].transform(
            lambda s: s.rolling(14, min_periods=5).mean())
        # vol_baseline = rolling MIN of prior 5 in-contract days. We use
        # min (not mean) so the baseline reflects the recent QUIET-day
        # volume — today's gap-day volume needs to beat that floor by
        # vol_mult to count as "visibly heavier than calm days." Using
        # mean was unstable because contract-initiation weeks carry
        # 3-5× normal volume that pulls the baseline up.
        daily['vol_baseline'] = daily.groupby('contract')['volume'].transform(
            lambda s: s.rolling(5, min_periods=3).min().shift(1))
    else:
        daily['is_roll'] = False
        daily['gap']     = daily['open'] - daily['close'].shift(1)
        daily['atr14']   = daily['range'].rolling(14, min_periods=5).mean()
        daily['vol_baseline'] = (daily['volume']
                                 .rolling(5, min_periods=3).min().shift(1))

    return daily


def _is_regime_shift(daily: pd.DataFrame, d,
                     prev_full_poc: float | None,
                     gap_atr: float, vol_mult: float,
                     accept_atr: float) -> bool:
    """
    Daily regime-shift test. ALL conditions must hold:
      1) |open_today − close_yesterday| ≥ gap_atr × ATR14
      2) today's volume ≥ vol_mult × prior 5-day mean volume
      3) today's close is at least accept_atr × ATR14 away from the
         end-of-yesterday POC of the running full profile
         (skipped if no POC available yet)
    """
    if d not in daily.index:
        return False
    row   = daily.loc[d]
    atr   = row.get('atr14',         np.nan)
    gap   = row.get('gap',           np.nan)
    vol   = row.get('volume',        np.nan)
    v5    = row.get('vol_baseline',  np.nan)
    close = row.get('close',         np.nan)
    is_roll = bool(row.get('is_roll', False))

    # Never flag a contract-roll day as a regime shift — the first day of
    # a new front-month always has gap-like price discontinuity vs the
    # expiring contract, but it's not a regime change in the auction.
    if is_roll:
        return False
    if (pd.isna(atr) or pd.isna(gap) or pd.isna(close) or atr <= 0):
        return False
    if abs(gap) < gap_atr * atr:
        return False
    # Volume confirmation is OPTIONAL (vol_mult=0 disables it). The
    # rolling baseline is fragile around contract starts because the
    # first few days of a fresh contract carry abnormally high volume
    # (position-establishment), so making it required produces false
    # negatives on otherwise-clean gap+acceptance days.
    if vol_mult > 0:
        if pd.isna(v5) or v5 <= 0 or vol < vol_mult * v5:
            return False
    if prev_full_poc is not None:
        if abs(close - prev_full_poc) < accept_atr * atr:
            return False
    return True


def detect_regime_shifts(data: pd.DataFrame,
                         bin_pts: float,
                         va_pct: float = 0.70,
                         gap_atr: float = 1.5,
                         vol_mult: float = 1.5,
                         accept_atr: float = 1.0) -> list:
    """
    Convenience helper: return the list of dates flagged as regime shifts
    when walking `data` left-to-right. Useful for charts and inspection
    outside the main backtest loop.
    """
    daily = _build_daily_summary(data)
    dates = list(daily.index)
    profile_full: dict = {}
    prev_poc: float | None = None
    shifts: list = []
    for d in dates:
        if _is_regime_shift(daily, d, prev_poc, gap_atr, vol_mult, accept_atr):
            shifts.append(d)
        # Grow profile_full with day d's bars, then snapshot POC
        day_df = data[data.index.date == d]
        for _, br in day_df.iterrows():
            _bar_distribute_volume(profile_full,
                                   float(br['Low']), float(br['High']),
                                   float(br.get('Volume', 0)), bin_pts)
        _, _, prev_poc, _ = _value_area(profile_full, bin_pts, va_pct)
    return shifts


# ── Strategy entry point ──────────────────────────────────────────────────────

def run_volume_profile(data: pd.DataFrame,
                       instrument_config: dict,
                       strategy_params: dict,
                       regime_df=None,
                       params=None,
                       signals_only: bool = False) -> pd.DataFrame:
    """
    Volume-profile retracement backtest with a dual full / sub-profile
    state machine.

    Modes
    -----
    signals_only=False  (default)
        Full simulation: emits one ROW PER COMPLETED TRADE with synthetic
        target/stop/squareoff exit applied. Used for P&L backtesting.

    signals_only=True
        Signal generator: emits one ROW PER ENTRY TRIGGER (no exit
        simulation). The trade-management rules (target/stop/EOD) are
        skipped — the user/discretionary trader picks their own exit.
        A cooldown of VP_SIGNAL_COOLDOWN_BARS suppresses duplicate same-
        direction signals.

    Trade record adds:
      profile_used   : 'sub' | 'full' — which profile drove the entry
      regime_start   : date the sub-profile began (NaT for 'full' rows)
    """
    LOT_SIZE  = instrument_config.get('lot_size',  15)
    BROKERAGE = instrument_config.get('brokerage', 40)

    def _p(key, default):
        if params is not None and key in params:
            return params[key]
        return strategy_params.get(key, default)

    VA_PCT             = float(_p('VP_VA_PCT',            0.70))
    BIN_PTS            = float(_p('VP_BIN_PTS',           20))
    MIN_PROFILE_DAYS   = int  (_p('VP_MIN_PROFILE_DAYS',  3))
    THRUST_LOOKBACK    = int  (_p('VP_THRUST_LOOKBACK_BARS', 1))
    THRUST_ATR         = float(_p('VP_THRUST_ATR',        0.5))
    VOL_MULT           = float(_p('VP_VOL_MULT',          2.0))
    MIN_PIERCE_PTS     = _p('VP_MIN_PIERCE_PTS', None)
    if MIN_PIERCE_PTS is None:
        MIN_PIERCE_PTS = BIN_PTS * 2
    MIN_PIERCE_PTS     = float(MIN_PIERCE_PTS)
    STOP_BUFFER_ATR    = float(_p('VP_STOP_BUFFER_ATR',   0.15))
    TARGET_KIND        = str  (_p('VP_TARGET',            'POC')).upper()
    ENTRY_WINDOW       = _p('VP_ENTRY_WINDOW',           ('10:00', '14:00'))
    SQUAREOFF          = _p('VP_SQUAREOFF',               '15:15')
    MAX_TRADES_DAY     = int  (_p('VP_MAX_TRADES_PER_DAY', 2))
    DOW_ALLOW          = _p('VP_DOW_ALLOW',               [0, 1, 2, 3, 4])

    REGIME_GAP_ATR    = float(_p('VP_REGIME_GAP_ATR',    1.5))
    REGIME_VOL_MULT   = float(_p('VP_REGIME_VOL_MULT',   1.5))
    REGIME_ACCEPT_ATR = float(_p('VP_REGIME_ACCEPT_ATR', 1.0))
    SUB_MIN_DAYS      = int  (_p('VP_SUB_MIN_DAYS',      2))

    # Failure-confirmation retrace: how far back toward the VA edge the
    # close must travel before we consider the breakout failed. 1.0 means
    # "all the way back inside VA" (strict, late entries). 0.5 means the
    # close has covered half the distance from the pierce extreme back
    # to VAH/VAL (earlier entries, more aggressive).
    FAIL_RETRACE_PCT  = float(_p('VP_FAIL_RETRACE_PCT',  0.50))

    # Target as a FRACTION of (entry → POC) distance. Empirically only
    # ~14% of trades reach POC and the median winning square-off captures
    # ~29% of the distance, so 0.5 (= POC midway) is far more reachable.
    TARGET_FRAC      = float(_p('VP_TARGET_FRAC',      0.50))

    # Skip thrusts that exceed this multiple of ATR14 — those are usually
    # accepted breakouts (trends), not failures we want to fade.
    THRUST_MAX_ATR   = float(_p('VP_THRUST_MAX_ATR',   1.0))

    # Once MFE reaches BE_TRIGGER_FRAC × target, move stop to entry.
    # Set to a value > 1.0 to disable the breakeven stop entirely.
    BE_TRIGGER_FRAC  = float(_p('VP_BE_TRIGGER_FRAC',  0.50))

    # Pre-entry bias-score filter: only take trades whose
    #   bias = min(pierce_pts / atr14, 1.0)
    # falls inside [BIAS_MIN, BIAS_MAX]. Lets a sweep keep the best
    # thrust-strength bucket (the loss-analysis showed bias 0.25-0.50
    # was the sweet spot at 60% WR).
    BIAS_MIN         = float(_p('VP_BIAS_MIN', 0.0))
    BIAS_MAX         = float(_p('VP_BIAS_MAX', 1.0))

    # Signals-only mode debounce — suppress further same-direction signals
    # for this many bars after one fires.
    SIGNAL_COOLDOWN  = int(_p('VP_SIGNAL_COOLDOWN_BARS', 8))

    MIN_T = dtime.fromisoformat(ENTRY_WINDOW[0])
    MAX_T = dtime.fromisoformat(ENTRY_WINDOW[1])
    SQ_T  = dtime.fromisoformat(SQUAREOFF)

    # ── Volume sanity ─────────────────────────────────────────────────────────
    if 'Volume' not in data.columns:
        print("  ⚠ volume_profile: no Volume column — skipping")
        return pd.DataFrame()
    sample_n = min(len(data), 26 * 20)
    if sample_n > 0:
        nz_pct = float((data['Volume'].head(sample_n) > 0).mean() * 100)
        if nz_pct < 10:
            print(f"  ⚠ volume_profile: Volume only {nz_pct:.0f}% non-zero — skipping")
            return pd.DataFrame()

    # ── Regime lookup (external macro labels — preserved) ────────────────────
    regime_lookup = {}
    if regime_df is not None:
        for _, row in regime_df.iterrows():
            regime_lookup[row['date']] = row.get('regime', 'neutral')

    # ── Daily summary for regime-shift test ──────────────────────────────────
    daily = _build_daily_summary(data)

    # ── Front-month roll detection ───────────────────────────────────────────
    has_contract = 'Contract' in data.columns
    contract_for_day: dict = {}
    if has_contract:
        for ts, c in data['Contract'].items():
            d = ts.date()
            if d not in contract_for_day:
                contract_for_day[d] = c

    dates = sorted(set(data.index.date))

    # ── Dual-profile state ───────────────────────────────────────────────────
    current_contract: str | None = None
    profile_full: dict = {}
    profile_full_days = 0
    profile_sub:  dict = {}
    profile_sub_days  = 0
    regime_start_date = None
    prev_full_poc: float | None = None
    n_shifts = 0

    records: list = []

    for di, tdate in enumerate(dates):
        day_df = data[data.index.date == tdate]
        if day_df.empty:
            continue

        # ── Contract roll → reset everything ─────────────────────────────────
        if has_contract:
            cfront = contract_for_day.get(tdate)
            if cfront != current_contract:
                current_contract  = cfront
                profile_full      = {}
                profile_full_days = 0
                profile_sub       = {}
                profile_sub_days  = 0
                regime_start_date = None
                prev_full_poc     = None

        # ── Regime-shift detection at start of each day ──────────────────────
        if _is_regime_shift(daily, tdate, prev_full_poc,
                            REGIME_GAP_ATR, REGIME_VOL_MULT, REGIME_ACCEPT_ATR):
            profile_sub       = {}
            profile_sub_days  = 0
            regime_start_date = tdate
            n_shifts         += 1

        # ── 14-day ATR (daily range, recomputed inline for cold start) ───────
        atr14 = float(daily.at[tdate, 'atr14']) \
            if (tdate in daily.index and not pd.isna(daily.at[tdate, 'atr14'])) \
            else 300.0
        if atr14 <= 0:
            atr14 = 300.0
        stop_buffer = atr14 * STOP_BUFFER_ATR
        thrust_min  = atr14 * THRUST_ATR

        # ── Cold start (first 15 days of contract) — grow only, no trades ────
        if di < 15 or profile_full_days < MIN_PROFILE_DAYS:
            for _, br in day_df.iterrows():
                _bar_distribute_volume(profile_full,
                                       float(br['Low']), float(br['High']),
                                       float(br.get('Volume', 0)), BIN_PTS)
                if regime_start_date is not None:
                    _bar_distribute_volume(profile_sub,
                                           float(br['Low']), float(br['High']),
                                           float(br.get('Volume', 0)), BIN_PTS)
            profile_full_days += 1
            if regime_start_date is not None:
                profile_sub_days += 1
            _, _, prev_full_poc, _ = _value_area(profile_full, BIN_PTS, VA_PCT)
            continue

        # ── DOW filter — no entries, but profiles still grow ─────────────────
        if DOW_ALLOW is not None and tdate.weekday() not in DOW_ALLOW:
            for _, br in day_df.iterrows():
                _bar_distribute_volume(profile_full,
                                       float(br['Low']), float(br['High']),
                                       float(br.get('Volume', 0)), BIN_PTS)
                if regime_start_date is not None:
                    _bar_distribute_volume(profile_sub,
                                           float(br['Low']), float(br['High']),
                                           float(br.get('Volume', 0)), BIN_PTS)
            profile_full_days += 1
            if regime_start_date is not None:
                profile_sub_days += 1
            _, _, prev_full_poc, _ = _value_area(profile_full, BIN_PTS, VA_PCT)
            continue

        bars = day_df.between_time('09:15', '15:30')
        if bars.empty:
            continue

        # Per-day trade state
        in_trade        = False
        entry_px        = None
        entry_ts        = None
        direction       = None
        stop_px         = None
        target_px       = None
        thrust_extreme  = None
        thrust_pts      = 0.0
        vol_mult_seen   = 0.0
        vah_at_entry    = None
        val_at_entry    = None
        poc_at_entry    = None
        profile_used_at_entry = None
        regime_start_at_entry = None
        be_active       = False     # breakeven stop tightened?
        trade_max_fav   = 0.0       # peak favourable excursion (pts)
        # signals-only debounce
        cooldown_bars_left = {-1: 0, 1: 0}  # per direction

        trades_today = 0
        regime_label = regime_lookup.get(tdate, 'neutral')
        intraday_vols: list = []
        kbuf = deque(maxlen=THRUST_LOOKBACK)

        for ts in bars.index:
            br      = bars.loc[ts]
            o, h, l, c = (float(br['Open']), float(br['High']),
                          float(br['Low']),  float(br['Close']))
            v       = float(br.get('Volume', 0))

            # ── Manage open trade first ───────────────────────────────────────
            if in_trade:
                exit_px     = None
                exit_reason = None

                # Update peak favourable excursion + arm breakeven stop
                # once MFE crosses BE_TRIGGER_FRAC × target distance.
                if direction == 1:
                    bar_fav = h - entry_px
                else:
                    bar_fav = entry_px - l
                if bar_fav > trade_max_fav:
                    trade_max_fav = bar_fav
                if not be_active and BE_TRIGGER_FRAC <= 1.0:
                    target_dist = abs(target_px - entry_px)
                    if (target_dist > 0
                            and trade_max_fav >= BE_TRIGGER_FRAC * target_dist):
                        # Tighten stop to entry — never loosen
                        if direction == 1:
                            stop_px = max(stop_px, entry_px)
                        else:
                            stop_px = min(stop_px, entry_px)
                        be_active = True

                if ts.time() >= SQ_T:
                    exit_px     = c
                    exit_reason = 'SQUARE OFF'
                elif direction == 1:
                    if l <= stop_px:
                        exit_px = stop_px
                        exit_reason = 'BREAKEVEN' if be_active else 'STOP LOSS'
                    elif h >= target_px:
                        exit_px = target_px; exit_reason = 'TARGET HIT'
                else:
                    if h >= stop_px:
                        exit_px = stop_px
                        exit_reason = 'BREAKEVEN' if be_active else 'STOP LOSS'
                    elif l <= target_px:
                        exit_px = target_px; exit_reason = 'TARGET HIT'

                if exit_px is not None:
                    pnl_pts = (exit_px - entry_px) * direction
                    pnl_rs  = round(pnl_pts * LOT_SIZE - BROKERAGE, 2)
                    bias    = round(min(thrust_pts / max(atr14, 1e-6), 1.0), 4)
                    records.append({
                        'date':         tdate,
                        'entry_ts':     entry_ts,
                        'exit_ts':      ts,
                        'year':         tdate.year,
                        'instrument':   instrument_config.get('symbol', ''),
                        'strategy':     'VOL_PROFILE',
                        'direction':    'LONG' if direction == 1 else 'SHORT',
                        'entry':        round(entry_px, 2),
                        'exit_price':   round(exit_px, 2),
                        'stop':         round(stop_px, 2),
                        'target':       round(target_px, 2),
                        'pnl_pts':      round(pnl_pts, 2),
                        'pnl_rs':       pnl_rs,
                        'win':          1 if pnl_rs > 0 else 0,
                        'exit_reason':  exit_reason,
                        'bias_score':   bias,
                        'lots_used':    LOT_SIZE,
                        'capital_used': instrument_config.get('margin_per_lot',
                                                              75_000),
                        'atr14':        round(atr14, 2),
                        'stop_pts':     round(abs(stop_px - entry_px), 2),
                        'target_pts':   round(abs(target_px - entry_px), 2),
                        'vah':          round(vah_at_entry, 2) if vah_at_entry is not None else None,
                        'val':          round(val_at_entry, 2) if val_at_entry is not None else None,
                        'poc':          round(poc_at_entry, 2) if poc_at_entry is not None else None,
                        'thrust_pts':   round(thrust_pts, 2),
                        'vol_mult':     round(vol_mult_seen, 2),
                        'profile_used': profile_used_at_entry,
                        'regime_start': regime_start_at_entry,
                        'profile_days': profile_full_days,
                        'regime':       regime_label,
                        'macro_ok':     True,
                    })
                    in_trade = False
                    trades_today += 1

            # ── Decrement cooldowns (signals-only mode) ───────────────────────
            for _dir in (-1, 1):
                if cooldown_bars_left[_dir] > 0:
                    cooldown_bars_left[_dir] -= 1

            # ── Look for new entry (only when flat, in window) ────────────────
            t_now = ts.time()
            scanning_for_entry = (
                signals_only or  # always scan in signals mode (no in_trade)
                (not in_trade and trades_today < MAX_TRADES_DAY)
            ) and (MIN_T <= t_now < MAX_T)
            if scanning_for_entry:

                # Pick which profile drives the trigger
                if (regime_start_date is not None
                        and profile_sub_days >= SUB_MIN_DAYS
                        and profile_sub):
                    active_profile        = profile_sub
                    profile_used_now      = 'sub'
                    regime_start_now      = regime_start_date
                else:
                    active_profile        = profile_full
                    profile_used_now      = 'full'
                    regime_start_now      = None

                vah, val, poc, _ = _value_area(active_profile, BIN_PTS, VA_PCT)
                if vah is not None and val is not None:
                    win = (list(kbuf) + [(ts, h, l, v)])[-THRUST_LOOKBACK:]
                    if len(win) >= THRUST_LOOKBACK:
                        win_high = max(w[1] for w in win)
                        win_low  = min(w[2] for w in win)
                        win_vol  = sum(w[3] for w in win)

                        prior_vols = (intraday_vols[:-(THRUST_LOOKBACK - 1)]
                                      if THRUST_LOOKBACK > 1
                                      else intraday_vols)
                        baseline = (np.mean(prior_vols)
                                    if len(prior_vols) >= 5 else 0.0)
                        vol_ok = (baseline > 0
                                  and win_vol >= VOL_MULT * baseline
                                  * THRUST_LOOKBACK)

                        thrust_max = atr14 * THRUST_MAX_ATR

                        def _bias_ok(pierce_pts: float) -> bool:
                            b = min(pierce_pts / max(atr14, 1e-6), 1.0)
                            return BIAS_MIN <= b <= BIAS_MAX

                        # Upside fade — pierce above VAH, then close has
                        # retraced FAIL_RETRACE_PCT of the way back toward VAH.
                        up_pierce = win_high - vah
                        if (up_pierce >= MIN_PIERCE_PTS
                                and up_pierce >= thrust_min
                                and up_pierce <= thrust_max
                                and _bias_ok(up_pierce)
                                and vol_ok):
                            up_retrace_level = win_high - FAIL_RETRACE_PCT * up_pierce
                            if c <= up_retrace_level:
                                # Target is a FRACTION of the distance from
                                # entry to the chosen "anchor" (POC or VA edge).
                                anchor = val if TARGET_KIND == 'VA_EDGE' else poc
                                tgt = c - TARGET_FRAC * (c - anchor)
                                if c - tgt >= MIN_PIERCE_PTS:
                                    direction      = -1
                                    entry_px       = c
                                    entry_ts       = ts
                                    thrust_extreme = win_high
                                    thrust_pts     = up_pierce
                                    vol_mult_seen  = (win_vol /
                                                      max(baseline * THRUST_LOOKBACK,
                                                          1e-9))
                                    stop_px   = thrust_extreme + stop_buffer
                                    target_px = tgt
                                    vah_at_entry, val_at_entry, poc_at_entry = (
                                        vah, val, poc)
                                    profile_used_at_entry = profile_used_now
                                    regime_start_at_entry = regime_start_now
                                    in_trade      = True
                                    be_active     = False
                                    trade_max_fav = 0.0

                        # Downside fade — pierce below VAL, then close has
                        # retraced FAIL_RETRACE_PCT toward VAL.
                        if not in_trade:
                            dn_pierce = val - win_low
                            if (dn_pierce >= MIN_PIERCE_PTS
                                    and dn_pierce >= thrust_min
                                    and dn_pierce <= thrust_max
                                    and _bias_ok(dn_pierce)
                                    and vol_ok):
                                dn_retrace_level = win_low + FAIL_RETRACE_PCT * dn_pierce
                                if c >= dn_retrace_level:
                                    anchor = vah if TARGET_KIND == 'VA_EDGE' else poc
                                    tgt = c + TARGET_FRAC * (anchor - c)
                                    if tgt - c >= MIN_PIERCE_PTS:
                                        direction      = 1
                                        entry_px       = c
                                        entry_ts       = ts
                                        thrust_extreme = win_low
                                        thrust_pts     = dn_pierce
                                        vol_mult_seen  = (win_vol /
                                                          max(baseline * THRUST_LOOKBACK,
                                                              1e-9))
                                        stop_px   = thrust_extreme - stop_buffer
                                        target_px = tgt
                                        vah_at_entry, val_at_entry, poc_at_entry = (
                                            vah, val, poc)
                                        profile_used_at_entry = profile_used_now
                                        regime_start_at_entry = regime_start_now
                                        in_trade      = True
                                        be_active     = False
                                        trade_max_fav = 0.0

                # ── signals-only conversion ───────────────────────────────────
                # When in signals_only mode, replace the simulated trade
                # with a signal-event record + cooldown so the same
                # direction can't re-fire for SIGNAL_COOLDOWN bars.
                if signals_only and in_trade:
                    if cooldown_bars_left[direction] > 0:
                        in_trade = False  # debounced — drop this signal
                    else:
                        records.append({
                            'date':           tdate,
                            'ts':             entry_ts,
                            'year':           tdate.year,
                            'instrument':     instrument_config.get('symbol', ''),
                            'contract':       current_contract,
                            'strategy':       'VP_SIGNAL',
                            'direction':      'LONG' if direction == 1 else 'SHORT',
                            'fade_kind':      'FADE_DOWN' if direction == 1 else 'FADE_UP',
                            'signal_price':   round(entry_px, 2),
                            'pierce_extreme': round(thrust_extreme, 2),
                            'pierce_pts':     round(thrust_pts, 2),
                            'pierce_atr':     round(thrust_pts / max(atr14, 1e-6), 3),
                            'bias_score':     round(min(thrust_pts / max(atr14, 1e-6), 1.0), 4),
                            'vah':            round(vah_at_entry, 2),
                            'val':            round(val_at_entry, 2),
                            'poc':            round(poc_at_entry, 2),
                            'profile_used':   profile_used_at_entry,
                            'regime_start':   regime_start_at_entry,
                            'atr14':          round(atr14, 2),
                            'vol_mult':       round(vol_mult_seen, 2),
                            'thrust_lookback_bars': THRUST_LOOKBACK,
                        })
                        cooldown_bars_left[direction] = SIGNAL_COOLDOWN
                        in_trade = False

            # ── EOD square-off if still open at SQ_T ──────────────────────────
            if in_trade and t_now >= SQ_T:
                pnl_pts = (c - entry_px) * direction
                pnl_rs  = round(pnl_pts * LOT_SIZE - BROKERAGE, 2)
                bias    = round(min(thrust_pts / max(atr14, 1e-6), 1.0), 4)
                records.append({
                    'date':         tdate,
                    'entry_ts':     entry_ts,
                    'exit_ts':      ts,
                    'year':         tdate.year,
                    'instrument':   instrument_config.get('symbol', ''),
                    'strategy':     'VOL_PROFILE',
                    'direction':    'LONG' if direction == 1 else 'SHORT',
                    'entry':        round(entry_px, 2),
                    'exit_price':   round(c, 2),
                    'stop':         round(stop_px, 2),
                    'target':       round(target_px, 2),
                    'pnl_pts':      round(pnl_pts, 2),
                    'pnl_rs':       pnl_rs,
                    'win':          1 if pnl_rs > 0 else 0,
                    'exit_reason':  'SQUARE OFF',
                    'bias_score':   bias,
                    'lots_used':    LOT_SIZE,
                    'capital_used': instrument_config.get('margin_per_lot',
                                                          75_000),
                    'atr14':        round(atr14, 2),
                    'stop_pts':     round(abs(stop_px - entry_px), 2),
                    'target_pts':   round(abs(target_px - entry_px), 2),
                    'vah':          round(vah_at_entry, 2) if vah_at_entry is not None else None,
                    'val':          round(val_at_entry, 2) if val_at_entry is not None else None,
                    'poc':          round(poc_at_entry, 2) if poc_at_entry is not None else None,
                    'thrust_pts':   round(thrust_pts, 2),
                    'vol_mult':     round(vol_mult_seen, 2),
                    'profile_used': profile_used_at_entry,
                    'regime_start': regime_start_at_entry,
                    'profile_days': profile_full_days,
                    'regime':       regime_label,
                    'macro_ok':     True,
                })
                in_trade = False
                trades_today += 1

            # ── Fold this bar's volume into BOTH profiles ────────────────────
            _bar_distribute_volume(profile_full, l, h, v, BIN_PTS)
            if regime_start_date is not None:
                _bar_distribute_volume(profile_sub, l, h, v, BIN_PTS)
            intraday_vols.append(v)
            kbuf.append((ts, h, l, v))

        profile_full_days += 1
        if regime_start_date is not None:
            profile_sub_days += 1
        _, _, prev_full_poc, _ = _value_area(profile_full, BIN_PTS, VA_PCT)

    if n_shifts:
        print(f"  • detected {n_shifts} regime shift(s) in this run")

    return pd.DataFrame(records)
