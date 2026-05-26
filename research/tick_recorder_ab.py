"""research/tick_recorder_ab.py — A/B compare v1 vs v2 tick capture quality.

Reads today's (or any given day's) v1 + v2 CSVs and prints a side-by-side
report on capture rate, qty=0 noise, cum_volume reversals, gap distribution,
and burst-row prevalence.

Run:    python research/tick_recorder_ab.py
        python research/tick_recorder_ab.py --date 2026-05-27 --inst NIFTY
"""
from __future__ import annotations

import argparse
import pathlib
from datetime import date

import pandas as pd
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / 'v3' / 'cache'


def _load(prefix: str, inst: str, day: str) -> pd.DataFrame | None:
    p = CACHE / f'{prefix}_{inst}_{day.replace("-","")}.csv'
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms', utc=True) \
        .dt.tz_convert('Asia/Kolkata')
    return df


def _report(label: str, df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {'label': label, 'rows': 0}
    # filter synthetic marker rows for fair stats
    real = df.copy()
    if 'side' in real.columns:
        real = real[~real['side'].isin(['GAP', 'RECONNECT'])]

    qty_zero  = int((real['qty'] == 0).sum())
    qty_pos   = real[real['qty'] > 0]
    cum_rev   = int((real['cum_volume'].diff() < 0).sum())
    gaps      = real['ts_ms'].diff()

    burst_rows = int((qty_pos['qty'] >= 50).sum()) if not qty_pos.empty else 0
    burst_vol  = float(qty_pos[qty_pos['qty'] >= 50]['qty'].sum()) \
                 if not qty_pos.empty else 0.0

    return {
        'label':            label,
        'rows':             int(len(df)),
        'rows_real':        int(len(real)),
        'qty_zero':         qty_zero,
        'qty_zero_pct':     100 * qty_zero / max(len(real), 1),
        'qty_sum':          float(qty_pos['qty'].sum()) if not qty_pos.empty else 0,
        'cum_vol_reversals': cum_rev,
        'gap_p50_s':         float(gaps.quantile(0.50) / 1000) if len(gaps) else 0,
        'gap_p90_s':         float(gaps.quantile(0.90) / 1000) if len(gaps) else 0,
        'gap_p99_s':         float(gaps.quantile(0.99) / 1000) if len(gaps) else 0,
        'gap_max_s':         float(gaps.max() / 1000)          if len(gaps) else 0,
        'burst_rows':        burst_rows,
        'burst_volume':      burst_vol,
        'burst_vol_share':   100 * burst_vol / max(qty_pos['qty'].sum(), 1)
                             if not qty_pos.empty else 0,
        'gap_markers':       int((df.get('side', pd.Series()) == 'GAP').sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat())
    ap.add_argument('--inst', default='NIFTY', choices=['NIFTY','BANKNIFTY','SENSEX'])
    args = ap.parse_args()

    v1 = _load('ticks',    args.inst, args.date)
    v2 = _load('ticks_v2', args.inst, args.date)

    r1 = _report('v1 (poll)',     v1)
    r2 = _report('v2 (callback)', v2)

    rows = [
        ('rows total',                   '{rows}'),
        ('rows (non-marker)',            '{rows_real}'),
        ('qty=0 rows',                   '{qty_zero} ({qty_zero_pct:.1f}%)'),
        ('captured qty sum (lots)',      '{qty_sum:,.0f}'),
        ('cum_volume reversals',         '{cum_vol_reversals}'),
        ('inter-row gap p50 (s)',        '{gap_p50_s:.2f}'),
        ('inter-row gap p90 (s)',        '{gap_p90_s:.2f}'),
        ('inter-row gap p99 (s)',        '{gap_p99_s:.2f}'),
        ('inter-row gap max (s)',        '{gap_max_s:.2f}'),
        ('"burst" rows (qty ≥ 50)',      '{burst_rows}'),
        ('  → volume in those',          '{burst_volume:,.0f}'),
        ('  → share of total',           '{burst_vol_share:.1f}%'),
        ('GAP markers written',          '{gap_markers}'),
    ]

    print(f'\nTick capture A/B — {args.inst} {args.date}\n')
    width = 32
    print(f'{"metric":<{width}}  {r1["label"]:>20}  {r2["label"]:>20}')
    print('-' * (width + 46))
    for name, fmt in rows:
        a = fmt.format(**r1) if r1.get('rows') else '—'
        b = fmt.format(**r2) if r2.get('rows') else '—'
        print(f'{name:<{width}}  {a:>20}  {b:>20}')
    print()

    # Quick interpretation
    if r1.get('rows') and r2.get('rows'):
        ratio = r2['rows_real'] / max(r1['rows_real'], 1)
        print(f'  v2/v1 row ratio: {ratio:.2f}×')
        if ratio < 1.2:
            print('  → Groww NATS relay is throttling upstream — no callback advantage.')
        elif ratio < 3:
            print('  → Modest gain. Real, but verify burst-row reduction tracks.')
        else:
            print('  → Material gain. Callback model recovers real lost data.')


if __name__ == '__main__':
    main()
