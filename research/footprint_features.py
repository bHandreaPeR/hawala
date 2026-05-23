"""research/footprint_features.py — Compute footprint context at a moment in time.

Given (instrument, timestamp), looks up the tick CSV for that day and
returns a dict of footprint features describing the state of order flow
just BEFORE the timestamp. Used by `research/footprint_correlation.py` to
attach footprint context to every historical / paper trade entry.

Features computed (over a lookback window, default 15 min):
    cvd_qty                 cumulative buy_qty - sell_qty in the window
    cvd_ticks               cumulative buy_ticks - sell_ticks
    cvd_direction           sign(cvd_ticks)  in [-1, 0, +1]
    n_buy_imbalances        count of cells where buy/sell ≥ IMB_MULT
    n_sell_imbalances       symmetric
    imb_ratio               (n_buy - n_sell) / max(n_buy+n_sell, 1)
    n_stacked_imbalances    sequences of ≥3 same-side adjacent imbalanced cells
    stacked_dir             dominant side of stacked imbalances (-1/0/+1)
    poc                     price level with most volume in window
    poc_dist_pts            ltp_at_ts - poc (positive = above POC)
    last_bar_delta          delta_qty of the most recent 1-min bar
    last_bar_body_pts       price.last - price.first of most recent 1-min bar
    last_bar_abs_ratio      abs(last_bar_delta) / abs(last_bar_body_pts)
                            (high ratio + small body = absorption signal)
    spread_at_ts            ask - bid at the closest tick
    spread_pct              spread / ltp
    ltp_at_ts               last price seen at-or-before ts

Returns dict with all keys above. If no tick CSV exists for that day,
returns all-zero / None values + a 'data_available'=False flag.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

ROOT      = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / 'v3' / 'cache'

DEFAULT_LOOKBACK_MIN  = 15
DEFAULT_CELL_SIZE     = {'NIFTY': 5.0, 'BANKNIFTY': 10.0, 'SENSEX': 10.0}
IMB_MULT              = 3
IMB_MIN_TICKS         = 4
STACK_MIN             = 3      # ≥3 same-side adjacent → "stacked"


# ─── Loader ──────────────────────────────────────────────────────────────────
def _load_ticks(inst: str, day: str) -> Optional[pd.DataFrame]:
    p = CACHE_DIR / f'ticks_{inst}_{day.replace("-","")}.csv'
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    if df.empty:
        return None
    df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms', utc=True).dt.tz_convert(
        'Asia/Kolkata').dt.tz_localize(None)
    return df.sort_values('ts').reset_index(drop=True)


# ─── Feature builder ─────────────────────────────────────────────────────────
def footprint_at(inst: str, ts: datetime, lookback_min: int = DEFAULT_LOOKBACK_MIN,
                 cell_size: Optional[float] = None) -> dict:
    """Compute footprint feature dict for `inst` at moment `ts`."""
    cs = cell_size or DEFAULT_CELL_SIZE.get(inst, 5.0)
    day = ts.strftime('%Y-%m-%d')
    ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts

    out = {
        'inst': inst, 'ts': str(ts_naive), 'lookback_min': lookback_min,
        'cell_size': cs, 'data_available': False,
        'cvd_qty': 0.0, 'cvd_ticks': 0, 'cvd_direction': 0,
        'n_buy_imbalances': 0, 'n_sell_imbalances': 0, 'imb_ratio': 0.0,
        'n_stacked_imbalances': 0, 'stacked_dir': 0,
        'poc': None, 'poc_dist_pts': None,
        'last_bar_delta': 0.0, 'last_bar_body_pts': 0.0, 'last_bar_abs_ratio': 0.0,
        'spread_at_ts': None, 'spread_pct': None,
        'ltp_at_ts': None, 'n_ticks_in_window': 0,
    }

    df = _load_ticks(inst, day)
    if df is None:
        return out

    win_start = ts_naive - timedelta(minutes=lookback_min)
    win = df[(df['ts'] >= win_start) & (df['ts'] <= ts_naive)].copy()
    if win.empty:
        return out

    out['data_available']     = True
    out['n_ticks_in_window']  = len(win)
    out['ltp_at_ts']          = float(win['price'].iloc[-1])
    out['spread_at_ts']       = float(win['spread'].iloc[-1])
    out['spread_pct']         = (out['spread_at_ts'] / out['ltp_at_ts']
                                 if out['ltp_at_ts'] else None)

    # CVD
    sign = win['side'].map({'BUY': 1, 'SELL': -1}).fillna(0).astype(int)
    out['cvd_qty']       = float((win['qty'] * sign).sum())
    out['cvd_ticks']     = int(sign.sum())
    out['cvd_direction'] = int(np.sign(out['cvd_ticks']))

    # Cell aggregation for imbalances + POC
    win['cell'] = (win['price'] / cs).round() * cs
    win['is_buy']  = (sign ==  1).astype(int)
    win['is_sell'] = (sign == -1).astype(int)
    cells = win.groupby('cell').agg(
        buy_ticks=('is_buy', 'sum'),
        sell_ticks=('is_sell','sum'),
        buy_qty=('qty', lambda x: x[sign.loc[x.index] == 1].sum() if len(x) else 0),
        sell_qty=('qty', lambda x: x[sign.loc[x.index] == -1].sum() if len(x) else 0),
    ).reset_index()
    cells['total_ticks'] = cells['buy_ticks'] + cells['sell_ticks']
    cells['total_qty']   = cells['buy_qty']   + cells['sell_qty']

    def _imb(r):
        if r['total_ticks'] < IMB_MIN_TICKS: return None
        if r['buy_ticks']  >= IMB_MULT * max(r['sell_ticks'], 1): return 'BUY'
        if r['sell_ticks'] >= IMB_MULT * max(r['buy_ticks'],  1): return 'SELL'
        return None
    cells['imbalance'] = cells.apply(_imb, axis=1)

    n_buy_imb  = int((cells['imbalance'] == 'BUY').sum())
    n_sell_imb = int((cells['imbalance'] == 'SELL').sum())
    total      = max(n_buy_imb + n_sell_imb, 1)
    out['n_buy_imbalances']  = n_buy_imb
    out['n_sell_imbalances'] = n_sell_imb
    out['imb_ratio']         = (n_buy_imb - n_sell_imb) / total

    # Stacked-imbalance scan (sorted by price)
    sorted_cells = cells.sort_values('cell').reset_index(drop=True)
    stacks = []
    run_side, run_start = None, None
    for i, r in sorted_cells.iterrows():
        if r['imbalance'] == run_side and run_side in ('BUY', 'SELL'):
            continue
        if run_side and run_start is not None:
            run_len = i - run_start
            if run_len >= STACK_MIN:
                stacks.append((run_side, run_len))
        run_side  = r['imbalance']
        run_start = i if r['imbalance'] in ('BUY', 'SELL') else None
    if run_side and run_start is not None:
        run_len = len(sorted_cells) - run_start
        if run_len >= STACK_MIN:
            stacks.append((run_side, run_len))
    out['n_stacked_imbalances'] = len(stacks)
    if stacks:
        # Dominant direction = the side with more total stacked cells
        buy_stacks  = sum(n for s, n in stacks if s == 'BUY')
        sell_stacks = sum(n for s, n in stacks if s == 'SELL')
        out['stacked_dir'] = 1 if buy_stacks > sell_stacks else \
                            (-1 if sell_stacks > buy_stacks else 0)

    # POC
    if cells['total_qty'].sum() > 0:
        poc_row = cells.loc[cells['total_qty'].idxmax()]
    else:
        poc_row = cells.loc[cells['total_ticks'].idxmax()]
    out['poc'] = float(poc_row['cell'])
    out['poc_dist_pts'] = float(out['ltp_at_ts'] - out['poc'])

    # Last 1-minute bar features (absorption signal)
    last_min_start = ts_naive - timedelta(minutes=1)
    last_bar = df[(df['ts'] >= last_min_start) & (df['ts'] <= ts_naive)]
    if not last_bar.empty:
        b_sign = last_bar['side'].map({'BUY': 1, 'SELL': -1}).fillna(0).astype(int)
        out['last_bar_delta']    = float((last_bar['qty'] * b_sign).sum())
        out['last_bar_body_pts'] = float(last_bar['price'].iloc[-1] -
                                         last_bar['price'].iloc[0])
        if abs(out['last_bar_body_pts']) > 0.01:
            out['last_bar_abs_ratio'] = (abs(out['last_bar_delta']) /
                                         abs(out['last_bar_body_pts']))

    return out


if __name__ == '__main__':
    # Smoke test — Friday's BN force-exit time
    f = footprint_at('BANKNIFTY', datetime(2026, 5, 22, 12, 50, 14))
    import json
    print(json.dumps(f, indent=2, default=str))
