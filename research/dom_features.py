"""research/dom_features.py — Compute DOM / liquidity context at a moment in time.

Mirrors `research/footprint_features.py` in shape and contract, but draws
on the top-5 market-depth CSVs written by `alerts/tick_recorder.py`
(`v3/cache/depth_<INST>_<YYYYMMDD>.csv`). Joined alongside footprint
features per trade entry to test whether resting-order context (walls,
imbalance, spread, persistence) carries predictive signal for win/loss.

Honest caveats baked into the design:
  - Top-5 only (Groww default). The deep book is invisible; spoofing
    detection is unreliable; icebergs only inferred from refresh patterns.
  - ~1Hz snapshot cadence → 500-1000ms blur vs the underlying tape.
  - Persistence/refresh metrics require ≥30s of snapshots to be meaningful;
    short lookbacks (<2min) will be noisy.

Features computed over a lookback window (default 5 min):

  Instantaneous (closest snapshot at-or-before ts):
    dom_bid_qty_top5         sum of qty across 5 bid levels
    dom_ask_qty_top5         sum of qty across 5 ask levels
    dom_imbalance            (bid - ask) / (bid + ask)   in [-1, +1]
    dom_spread_pts           best_ask - best_bid
    dom_best_bid_price       price of level-1 bid
    dom_best_ask_price       price of level-1 ask
    dom_best_bid_qty         qty at level-1 bid
    dom_best_ask_qty         qty at level-1 ask

  Window aggregates:
    dom_n_snapshots          count of snapshots in window
    dom_mean_bid_qty         mean top-5 bid qty
    dom_mean_ask_qty         mean top-5 ask qty
    dom_mean_imbalance       mean imbalance
    dom_imbalance_trend      OLS slope of imbalance vs time (per minute)
    dom_spread_volatility    stdev of spread (pts)

  Persistence / walls (per-price-level over the window):
    dom_persistent_bid_price price of the bid level that appeared in
                              ≥PERSIST_FRAC of snapshots; None if none
    dom_persistent_bid_qty   mean qty at that persistent bid
    dom_persistent_ask_price symmetric for ask
    dom_persistent_ask_qty
    dom_largest_bid_wall     max qty observed at any bid level in window
    dom_largest_ask_wall     max qty observed at any ask level in window
    dom_largest_wall_dist    abs(ltp - price_of_largest_wall) in pts.
                              Negative if wall is on ask side (above ltp),
                              positive if on bid side (below ltp).
    dom_max_refresh_count    max # times a single price level's qty
                              bounced back to ≥REFRESH_RATIO of its max
                              after dipping ≤(1-REFRESH_DIP). Crude
                              iceberg / market-maker signal.

If no depth CSV exists for that day OR no snapshots fall in the window,
returns all-zero / None values + `dom_data_available=False`.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

ROOT      = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / 'v3' / 'cache'

DEFAULT_LOOKBACK_MIN = 5
PERSIST_FRAC         = 0.70   # level must appear in ≥70 % of snapshots
REFRESH_RATIO        = 0.70   # qty bounces back to ≥70 % of max
REFRESH_DIP          = 0.30   # after dipping by ≥30 %


# ─── Loader ──────────────────────────────────────────────────────────────────
def _load_depth(inst: str, day: str) -> Optional[pd.DataFrame]:
    p = CACHE_DIR / f'depth_{inst}_{day.replace("-","")}.csv'
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    if df.empty:
        return None
    # The recorder occasionally double-writes the same (ts, level, side) row.
    # Dedup so window aggregates aren't biased.
    df = df.drop_duplicates(subset=['ts_ms', 'level', 'side'], keep='last')
    df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms', utc=True).dt.tz_convert(
        'Asia/Kolkata').dt.tz_localize(None)
    return df.sort_values(['ts_ms', 'level']).reset_index(drop=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _per_snapshot_aggs(win: pd.DataFrame) -> pd.DataFrame:
    """Collapse a (ts_ms, level, side, price, qty) frame to one row per
    snapshot: bid/ask top-5 totals, best price/qty, spread, imbalance."""
    bids = win[win['side'] == 'BID']
    asks = win[win['side'] == 'ASK']

    bid_sum = bids.groupby('ts_ms')['qty'].sum().rename('bid_qty')
    ask_sum = asks.groupby('ts_ms')['qty'].sum().rename('ask_qty')

    bid_best = bids[bids['level'] == 1].set_index('ts_ms')[['price', 'qty']] \
        .rename(columns={'price': 'best_bid_price', 'qty': 'best_bid_qty'})
    ask_best = asks[asks['level'] == 1].set_index('ts_ms')[['price', 'qty']] \
        .rename(columns={'price': 'best_ask_price', 'qty': 'best_ask_qty'})

    snap = pd.concat([bid_sum, ask_sum, bid_best, ask_best], axis=1).dropna()
    if snap.empty:
        return snap
    snap['spread']    = snap['best_ask_price'] - snap['best_bid_price']
    snap['imbalance'] = (snap['bid_qty'] - snap['ask_qty']) / \
                       (snap['bid_qty'] + snap['ask_qty']).clip(lower=1e-9)
    snap = snap.reset_index().sort_values('ts_ms').reset_index(drop=True)
    return snap


def _persistence(win: pd.DataFrame, side: str, n_snaps: int) -> tuple:
    """Return (persistent_price, mean_qty_there) for the requested side, or
    (None, None) if no price level was present in ≥PERSIST_FRAC of snapshots."""
    sub = win[win['side'] == side]
    if sub.empty or n_snaps == 0:
        return None, None
    # Round price to nearest 0.05 to coalesce floating-point jitter into
    # stable level identities.
    sub = sub.assign(price_bin=(sub['price'] * 20).round() / 20.0)
    counts = sub.groupby('price_bin')['ts_ms'].nunique()
    hits   = counts[counts >= PERSIST_FRAC * n_snaps]
    if hits.empty:
        return None, None
    # Pick the persistent level with the highest MEAN qty (real wall, not
    # just a sticky one-lot order).
    means = sub[sub['price_bin'].isin(hits.index)] \
        .groupby('price_bin')['qty'].mean()
    best = means.idxmax()
    return float(best), float(means.loc[best])


def _refresh_count(qty_series: pd.Series) -> int:
    """How many times does this price-level's qty bounce back to ≥
    REFRESH_RATIO of its running max after dipping by ≥REFRESH_DIP fraction?
    Crude iceberg / market-maker signal."""
    if len(qty_series) < 4:
        return 0
    arr = qty_series.values.astype(float)
    peak = arr[0] if arr[0] > 0 else 1.0
    dipped = False
    n = 0
    for v in arr[1:]:
        if v <= peak * (1.0 - REFRESH_DIP):
            dipped = True
        elif dipped and v >= peak * REFRESH_RATIO:
            n += 1
            dipped = False
            peak = max(peak, v)
        else:
            peak = max(peak, v)
    return n


# ─── Public entrypoint ───────────────────────────────────────────────────────
def dom_at(inst: str, ts: datetime,
           lookback_min: int = DEFAULT_LOOKBACK_MIN,
           ltp: Optional[float] = None) -> dict:
    """Compute DOM feature dict for `inst` at moment `ts`.

    Args
    ----
    inst         : 'NIFTY' / 'BANKNIFTY' / 'SENSEX'
    ts           : timestamp (naive or tz-aware; coerced to naive Asia/Kolkata)
    lookback_min : window for persistence/trend metrics
    ltp          : optional last traded price for wall-distance metric.
                   If None, uses the at-ts mid-price (best_bid + best_ask) / 2.
    """
    day = ts.strftime('%Y-%m-%d')
    ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts

    out = {
        'inst': inst, 'ts': str(ts_naive), 'lookback_min': lookback_min,
        'dom_data_available': False,
        # instantaneous
        'dom_bid_qty_top5': 0.0, 'dom_ask_qty_top5': 0.0,
        'dom_imbalance': 0.0, 'dom_spread_pts': None,
        'dom_best_bid_price': None, 'dom_best_ask_price': None,
        'dom_best_bid_qty':  0.0,  'dom_best_ask_qty':  0.0,
        # window aggregates
        'dom_n_snapshots': 0,
        'dom_mean_bid_qty': 0.0, 'dom_mean_ask_qty': 0.0,
        'dom_mean_imbalance': 0.0, 'dom_imbalance_trend': 0.0,
        'dom_spread_volatility': 0.0,
        # persistence / walls
        'dom_persistent_bid_price': None, 'dom_persistent_bid_qty': 0.0,
        'dom_persistent_ask_price': None, 'dom_persistent_ask_qty': 0.0,
        'dom_largest_bid_wall': 0.0, 'dom_largest_ask_wall': 0.0,
        'dom_largest_wall_dist': None,
        'dom_max_refresh_count': 0,
    }

    df = _load_depth(inst, day)
    if df is None:
        return out

    win_start = ts_naive - timedelta(minutes=lookback_min)
    win = df[(df['ts'] >= win_start) & (df['ts'] <= ts_naive)].copy()
    if win.empty:
        return out

    snaps = _per_snapshot_aggs(win)
    if snaps.empty:
        return out

    out['dom_data_available'] = True
    out['dom_n_snapshots']    = int(len(snaps))

    # ── Instantaneous (closest snapshot at or before ts) ────────────────────
    last = snaps.iloc[-1]
    out['dom_bid_qty_top5']   = float(last['bid_qty'])
    out['dom_ask_qty_top5']   = float(last['ask_qty'])
    out['dom_imbalance']      = float(last['imbalance'])
    out['dom_spread_pts']     = float(last['spread'])
    out['dom_best_bid_price'] = float(last['best_bid_price'])
    out['dom_best_ask_price'] = float(last['best_ask_price'])
    out['dom_best_bid_qty']   = float(last['best_bid_qty'])
    out['dom_best_ask_qty']   = float(last['best_ask_qty'])

    # ── Window aggregates ───────────────────────────────────────────────────
    out['dom_mean_bid_qty']      = float(snaps['bid_qty'].mean())
    out['dom_mean_ask_qty']      = float(snaps['ask_qty'].mean())
    out['dom_mean_imbalance']    = float(snaps['imbalance'].mean())
    out['dom_spread_volatility'] = float(snaps['spread'].std(ddof=0))

    if len(snaps) >= 3:
        t = (snaps['ts_ms'].values - snaps['ts_ms'].iloc[0]) / 60_000.0  # min
        y = snaps['imbalance'].values
        if t[-1] > 0:
            slope = float(np.polyfit(t, y, 1)[0])
            out['dom_imbalance_trend'] = slope

    # ── Persistence ─────────────────────────────────────────────────────────
    n_snaps = int(snaps['ts_ms'].nunique())
    out['dom_persistent_bid_price'], out['dom_persistent_bid_qty'] = \
        _persistence(win, 'BID', n_snaps)
    out['dom_persistent_ask_price'], out['dom_persistent_ask_qty'] = \
        _persistence(win, 'ASK', n_snaps)

    # ── Walls (max qty seen at any single level) ────────────────────────────
    bids = win[win['side'] == 'BID']
    asks = win[win['side'] == 'ASK']
    if not bids.empty:
        out['dom_largest_bid_wall'] = float(bids['qty'].max())
    if not asks.empty:
        out['dom_largest_ask_wall'] = float(asks['qty'].max())

    # Largest-wall-side comparison and distance from ltp/mid
    mid = ltp if ltp is not None else \
          (float(last['best_bid_price']) + float(last['best_ask_price'])) / 2.0
    if (out['dom_largest_bid_wall'] > 0 or out['dom_largest_ask_wall'] > 0):
        if out['dom_largest_bid_wall'] >= out['dom_largest_ask_wall']:
            wall_px = float(bids.loc[bids['qty'].idxmax(), 'price'])
            out['dom_largest_wall_dist'] = float(mid - wall_px)   # +ve (below)
        else:
            wall_px = float(asks.loc[asks['qty'].idxmax(), 'price'])
            out['dom_largest_wall_dist'] = float(mid - wall_px)   # -ve (above)

    # ── Refresh count (iceberg-ish) ─────────────────────────────────────────
    # Pivot: for each (side, level) over time, compute refresh count and take
    # the global max. This is a noisy estimator; treat as a tie-break feature.
    max_refresh = 0
    for (side, lvl), sub in win.groupby(['side', 'level']):
        sub = sub.sort_values('ts_ms')
        rc = _refresh_count(sub['qty'])
        if rc > max_refresh:
            max_refresh = rc
    out['dom_max_refresh_count'] = int(max_refresh)

    return out


# ─── Smoke test ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import json
    # Today, mid-morning — should have rich data
    f = dom_at('NIFTY', datetime(2026, 5, 26, 9, 55, 0))
    print(json.dumps(f, indent=2, default=str))
