"""research/positioning_leading.py — Does the composite (or any single
component) actually lead price?

Reads v3/cache/positioning_<INST>_<YYYYMMDD>.ndjson, joins each snapshot
with the LTP N minutes later (from the same ticks CSV the viewer logged
against), and reports — per component and for the composite:

    n_samples         number of snapshots observed
    hit_rate          fraction where sign(forward_return) == sign(score)
    mean_fwd_ret      mean forward return in the direction of the score
    information_coef  Spearman rank correlation between score and fwd_ret
                       (the standard 'IC' used in factor research)

By default it looks 5 / 15 / 30 minutes forward — the horizons the
positioning view is supposed to be useful over. A component with
hit_rate > 0.55 and IC > 0.05 is interesting; under that, it's noise.

Run:
    python research/positioning_leading.py
    python research/positioning_leading.py --inst BANKNIFTY --horizon-min 10
    python research/positioning_leading.py --dates 2026-05-27,2026-05-28
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import date, datetime

import pandas as pd
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / 'v3' / 'cache'


def _load_positioning(inst: str, day: str) -> pd.DataFrame:
    """Flatten NDJSON snapshots into a tidy DataFrame."""
    p = CACHE / f'positioning_{inst}_{day.replace("-","")}.ndjson'
    if not p.exists():
        return pd.DataFrame()
    rows = []
    with p.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            rows.append({
                'ts_ms':         d.get('ts_ms'),
                'ltp':           d.get('ltp'),
                'flow':          d.get('flow',         {}).get('value', 0),
                'flow_dir':      d.get('flow',         {}).get('dir',   0),
                'resting':       d.get('resting',      {}).get('value', 0),
                'resting_dir':   d.get('resting',      {}).get('dir',   0),
                'inst':          d.get('institutions', {}).get('value', 0),
                'inst_dir':      d.get('institutions', {}).get('dir',   0),
                'macro':         d.get('macro',        {}).get('value', 0),
                'macro_dir':     d.get('macro',        {}).get('dir',   0),
                'composite':     d.get('composite',    {}).get('value', 0),
                'composite_dir': d.get('composite',    {}).get('dir',   0),
                'aligned':       d.get('composite',    {}).get('aligned', False),
            })
    df = pd.DataFrame(rows).dropna(subset=['ts_ms', 'ltp'])
    df = df.sort_values('ts_ms').reset_index(drop=True)
    return df


def _load_ticks(inst: str, day: str) -> pd.DataFrame:
    p = CACHE / f'ticks_{inst}_{day.replace("-","")}.csv'
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    return df.sort_values('ts_ms').reset_index(drop=True)


def _attach_forward_returns(pos: pd.DataFrame, ticks: pd.DataFrame,
                             horizons_min: list[int]) -> pd.DataFrame:
    """For each positioning row, find the LTP `horizon` minutes later and
    compute the forward return."""
    if pos.empty or ticks.empty:
        return pos
    tick_ts = ticks['ts_ms'].values
    tick_px = ticks['price'].values
    for h in horizons_min:
        target_ts = pos['ts_ms'].values + h * 60_000
        idx = np.searchsorted(tick_ts, target_ts, side='left')
        idx = np.clip(idx, 0, len(tick_px) - 1)
        fwd_px = tick_px[idx]
        # Only valid if the tick we used is actually >= target_ts
        valid = tick_ts[idx] >= target_ts
        ret = (fwd_px - pos['ltp'].values) / pos['ltp'].values
        ret[~valid] = np.nan
        pos[f'fwd_ret_{h}m'] = ret
    return pos


def _evaluate(pos: pd.DataFrame, score_col: str, dir_col: str,
              h: int) -> dict:
    fwd = pos[f'fwd_ret_{h}m']
    s   = pos[score_col]
    d   = pos[dir_col]
    mask = fwd.notna()
    if not mask.any():
        return {'n': 0}
    sub_fwd = fwd[mask]
    sub_s   = s[mask]
    sub_d   = d[mask]
    # Hit rate among non-zero direction rows only
    nz = sub_d != 0
    if nz.any():
        hit = float(((np.sign(sub_fwd[nz]) == sub_d[nz])).mean())
        signed_fwd = float((sub_fwd[nz] * sub_d[nz]).mean())
    else:
        hit, signed_fwd = float('nan'), float('nan')
    # Spearman IC: rank correlation between score and forward return
    try:
        ic = float(pd.Series(sub_s).rank().corr(pd.Series(sub_fwd.values).rank()))
    except Exception:
        ic = float('nan')
    return {
        'n':              int(mask.sum()),
        'n_nonflat':      int(nz.sum()),
        'hit_rate':       hit,
        'mean_fwd_ret':   signed_fwd,
        'mean_fwd_ret_bp': signed_fwd * 1e4 if signed_fwd == signed_fwd else float('nan'),
        'ic':             ic,
    }


def _report(pos: pd.DataFrame, horizons: list[int]) -> None:
    components = [
        ('FLOW',         'flow',      'flow_dir'),
        ('RESTING',      'resting',   'resting_dir'),
        ('INSTITUTIONS', 'inst',      'inst_dir'),
        ('MACRO',        'macro',     'macro_dir'),
        ('COMPOSITE',    'composite', 'composite_dir'),
    ]
    print(f'\nLeading-direction evaluation — {len(pos):,} snapshots\n')
    header = f'{"component":<14}'
    for h in horizons:
        header += f'  hit_{h}m  ret_{h}m(bp)  IC_{h}m'
    print(header)
    print('-' * len(header))
    for name, sc, dc in components:
        row = f'{name:<14}'
        for h in horizons:
            r = _evaluate(pos, sc, dc, h)
            if r['n'] == 0 or r.get('n_nonflat', 0) < 10:
                row += f'  {"—":>6}  {"—":>9}  {"—":>5}'
            else:
                row += (f'  {r["hit_rate"]:>5.1%}'
                        f'  {r["mean_fwd_ret_bp"]:>+9.1f}'
                        f'  {r["ic"]:>+5.3f}')
        print(row)
    # Special: composite + aligned subset (only when all 4 components agreed)
    aligned = pos[pos['aligned'] == True]   # noqa: E712
    if len(aligned) >= 10:
        print('\n  -- composite WHEN aligned (gold-halo moments) --')
        for h in horizons:
            r = _evaluate(aligned, 'composite', 'composite_dir', h)
            if r['n'] == 0:
                continue
            print(f'    {h:>2}m: n={r["n_nonflat"]:>4}  '
                  f'hit={r["hit_rate"]:>5.1%}  '
                  f'ret={r["mean_fwd_ret_bp"]:>+6.1f}bp  IC={r["ic"]:>+5.3f}')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--inst', default='NIFTY',
                    choices=['NIFTY', 'BANKNIFTY', 'SENSEX'])
    ap.add_argument('--dates', default=date.today().isoformat(),
                    help='comma-separated YYYY-MM-DD list')
    ap.add_argument('--horizons', default='5,15,30',
                    help='comma-separated minutes forward')
    args = ap.parse_args()

    days = [d.strip() for d in args.dates.split(',') if d.strip()]
    horizons = [int(h) for h in args.horizons.split(',') if h.strip()]

    all_pos = []
    for d in days:
        pos = _load_positioning(args.inst, d)
        ticks = _load_ticks(args.inst, d)
        if pos.empty or ticks.empty:
            print(f'  [{d}] {args.inst}: pos={len(pos)} ticks={len(ticks)} — skip')
            continue
        pos = _attach_forward_returns(pos, ticks, horizons)
        all_pos.append(pos)
        print(f'  [{d}] {args.inst}: {len(pos):,} snapshots, {len(ticks):,} ticks')

    if not all_pos:
        print('no data found'); return

    df = pd.concat(all_pos, ignore_index=True)
    _report(df, horizons)
    print('\nKey: hit = sign(fwd_ret) == sign(score) | ret = mean fwd return '
          'in score direction (bp) | IC = Spearman rank corr.')
    print('  Interesting threshold: hit > 55 %  and  IC > +0.05')


if __name__ == '__main__':
    main()
