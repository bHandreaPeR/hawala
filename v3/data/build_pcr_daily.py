"""v3/data/build_pcr_daily.py — Rebuild pcr_daily.csv from the bhavcopy cache.

Why this exists
---------------
`pcr_daily.csv` is the daily NIFTY put-call-ratio table the signal engine and
healthcheck both read. It used to be written ONLY as a side-effect of the
NIFTY runner's morning bhavcopy fetch (`runner_nifty._refresh_morning_bhavcopy`).

But `ops/autoheal.py` runs at 06:55 and pre-fetches the bhavcopy `.pkl`. When
the runner starts at 09:12 it sees yesterday already cached, takes the
"skipping fetch" early-return path, and never writes `pcr_daily.csv`. Result:
the file silently goes stale even though the underlying bhavcopy is fresh.

This script is the authoritative, standalone PCR-table writer. It reads the
already-fetched `bhavcopy_NIFTY_all.pkl`, computes per-date PCR, and writes
`pcr_daily.csv` — merging with (not discarding) any pre-existing rows for
dates not present in the pkl.

Run:  python v3/data/build_pcr_daily.py
Used by: ops/autoheal.py  (PCR-daily fixer)
"""
from __future__ import annotations

import pickle
import pathlib

import pandas as pd

ROOT       = pathlib.Path(__file__).resolve().parents[2]
BHAV_CACHE = ROOT / 'v3' / 'cache' / 'bhavcopy_NIFTY_all.pkl'
PCR_CACHE  = ROOT / 'v3' / 'cache' / 'pcr_daily.csv'


def build_pcr_daily() -> str:
    """Rebuild pcr_daily.csv from bhavcopy_NIFTY_all.pkl. Returns a status line."""
    if not BHAV_CACHE.exists():
        raise RuntimeError(f"bhavcopy cache missing: {BHAV_CACHE}")

    with open(BHAV_CACHE, 'rb') as fh:
        cache = pickle.load(fh)
    if not isinstance(cache, dict) or not cache:
        raise RuntimeError(f"bhavcopy cache empty or malformed: {BHAV_CACHE}")

    # Per-date PCR from the pkl
    rows = []
    for d_str, df_s in cache.items():
        if df_s is None or getattr(df_s, 'empty', True):
            continue
        if 'ce_oi' not in df_s.columns or 'pe_oi' not in df_s.columns:
            continue
        ce_tot = float(df_s['ce_oi'].sum())
        pe_tot = float(df_s['pe_oi'].sum())
        if ce_tot > 0:
            rows.append({'date': str(d_str)[:10],
                         'pcr': round(pe_tot / ce_tot, 4)})
    if not rows:
        raise RuntimeError("no usable bhavcopy rows — cannot build PCR table")

    pkl_df = pd.DataFrame(rows)
    pkl_dates = set(pkl_df['date'])

    # Merge: keep pre-existing csv rows for dates the pkl doesn't cover
    if PCR_CACHE.exists():
        try:
            old = pd.read_csv(PCR_CACHE)
            old['date'] = old['date'].astype(str).str[:10]
            old = old[~old['date'].isin(pkl_dates)][['date', 'pcr']]
        except Exception:
            old = pd.DataFrame(columns=['date', 'pcr'])
    else:
        old = pd.DataFrame(columns=['date', 'pcr'])

    out = pd.concat([old, pkl_df], ignore_index=True)
    out = out.drop_duplicates('date', keep='last').sort_values('date')
    out = out.reset_index(drop=True)
    out['pcr_5d_ma'] = out['pcr'].rolling(5, min_periods=1).mean()
    out.to_csv(PCR_CACHE, index=False)

    last = out['date'].iloc[-1]
    return (f"pcr_daily.csv rebuilt — {len(out)} rows, last={last}, "
            f"pcr={out['pcr'].iloc[-1]:.4f}")


if __name__ == '__main__':
    print(build_pcr_daily())
