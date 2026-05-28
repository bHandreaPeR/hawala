"""ops/stitch_trading_day.py — Reassemble a tick file that got split by
mid-session recorder restarts into one complete day the viewer can read.

Background: on days with recorder restarts (e.g. 2026-05-27), the morning
ends up in `ticks_<INST>_<DATE>.csv.preBackport` (old v1 schema) and/or
`ticks_v2_<INST>_<DATE>.csv` (clean v2 schema), while the afternoon is in
the current `ticks_<INST>_<DATE>.csv`. The viewer only reads the current
file, so the morning is invisible.

This tool merges, time-partitioned (no double-counting), into one 17-col
file the viewer reads:
    early    : preBackport rows BEFORE v2 started (the ~10 min v2 missed)
    morning  : v2 rows (cleaner — qty=0 filtered, reversals handled)
    afternoon: current file rows
A GAP marker row (side=GAP, qty=0) is inserted across any hole > GAP_MIN so
research can tell 'no data' from 'quiet market'.

Non-destructive: the current file is backed up to `.afternoon` before the
merged file is written. preBackport + v2 are left untouched.

Run:
    python -m ops.stitch_trading_day --date 2026-05-27
    python -m ops.stitch_trading_day --date 2026-05-27 --inst NIFTY --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
import pathlib
import warnings
from datetime import date

import pandas as pd

warnings.filterwarnings('ignore')

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / 'v3' / 'cache'

# Canonical 17-col schema (patched v1 / v2).
COLS17 = ['ts_ms', 'inst', 'price', 'qty', 'side', 'rule',
          'bid', 'ask', 'bid_qty', 'ask_qty', 'spread', 'cum_volume',
          'microprice', 'notional', 'aggression', 'gap_ms', 'msg_seq']
GAP_MIN_MS = 5 * 60 * 1000   # ≥5-min hole → insert a GAP marker


def _load(path: pathlib.Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLS17)
    df = pd.read_csv(path, on_bad_lines='skip')
    # Pad 12-col (old v1) to 17-col
    if 'microprice' not in df.columns:
        df['microprice'] = ((df.get('bid', 0).fillna(0) + df.get('ask', 0).fillna(0)) / 2)
        df['notional']   = df.get('qty', 0).fillna(0) * df.get('price', 0).fillna(0)
        df['aggression'] = 0.0
        df['gap_ms']     = 0
        df['msg_seq']    = 0
    # Keep only the canonical columns, in order
    for c in COLS17:
        if c not in df.columns:
            df[c] = 0
    return df[COLS17]


def stitch(inst: str, day: date, dry_run: bool) -> None:
    ymd = day.strftime('%Y%m%d')
    cur_p  = CACHE / f'ticks_{inst}_{ymd}.csv'
    v2_p   = CACHE / f'ticks_v2_{inst}_{ymd}.csv'
    pre_p  = CACHE / f'ticks_{inst}_{ymd}.csv.preBackport'

    cur = _load(cur_p)      # afternoon
    v2  = _load(v2_p)       # clean morning
    pre = _load(pre_p)      # old-v1 morning (for the early bit only)

    if v2.empty and pre.empty:
        print(f'  {inst}: no morning source (v2 + preBackport both empty) — skip')
        return
    if cur.empty:
        print(f'  {inst}: no current/afternoon file — skip')
        return

    # Time partitions — no overlap, so no double-counting:
    #   early   = preBackport rows strictly before v2's first ts
    #   morning = all of v2
    #   after   = current file rows strictly after v2's last ts
    parts = []
    if not v2.empty:
        v2_start = int(v2['ts_ms'].min())
        v2_end   = int(v2['ts_ms'].max())
        if not pre.empty:
            early = pre[pre['ts_ms'] < v2_start]
            if not early.empty:
                parts.append(early)
        parts.append(v2)
        after = cur[cur['ts_ms'] > v2_end]
        parts.append(after)
    else:
        # No v2 — use preBackport morning + current afternoon
        pre_end = int(pre['ts_ms'].max())
        parts.append(pre)
        parts.append(cur[cur['ts_ms'] > pre_end])

    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(subset=['ts_ms'], keep='first')
    merged = merged.sort_values('ts_ms').reset_index(drop=True)

    # Insert GAP markers across holes > GAP_MIN_MS
    gaps = merged['ts_ms'].diff()
    gap_rows = []
    for i in merged.index[1:]:
        g = gaps.loc[i]
        if g and g > GAP_MIN_MS:
            prev_ts = int(merged.loc[i-1, 'ts_ms'])
            mid = prev_ts + int(g // 2)
            r = {c: 0 for c in COLS17}
            r.update({'ts_ms': mid, 'inst': inst, 'side': 'GAP',
                      'rule': 'GAP', 'price': float(merged.loc[i, 'price']),
                      'gap_ms': int(g)})
            gap_rows.append(r)
    if gap_rows:
        merged = pd.concat([merged, pd.DataFrame(gap_rows)], ignore_index=True)
        merged = merged.sort_values('ts_ms').reset_index(drop=True)

    t0 = pd.to_datetime(merged['ts_ms'].min(), unit='ms', utc=True).tz_convert('Asia/Kolkata')
    t1 = pd.to_datetime(merged['ts_ms'].max(), unit='ms', utc=True).tz_convert('Asia/Kolkata')
    qpos = int((merged['qty'] > 0).sum())
    print(f'  {inst}: merged {len(merged)} rows ({qpos} qty>0, {len(gap_rows)} GAP markers) '
          f'spanning {t0.strftime("%H:%M")} → {t1.strftime("%H:%M")}')

    if dry_run:
        print(f'  {inst}: [dry-run] would write {cur_p.name} (current backed up to .afternoon)')
        return

    # Non-destructive: back up the current afternoon-only file first
    backup = cur_p.with_suffix('.csv.afternoon')
    if not backup.exists():
        shutil.copy2(cur_p, backup)
        print(f'  {inst}: backed up current → {backup.name}')
    merged.to_csv(cur_p, index=False)
    print(f'  {inst}: wrote unified day → {cur_p.name}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--inst', default='ALL',
                    help='NIFTY / BANKNIFTY / SENSEX / ALL (default ALL)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    day = date.fromisoformat(args.date)
    insts = ['NIFTY', 'BANKNIFTY', 'SENSEX'] if args.inst == 'ALL' else [args.inst]
    print(f'stitch_trading_day {args.date} insts={insts} dry_run={args.dry_run}')
    for inst in insts:
        try:
            stitch(inst, day, args.dry_run)
        except Exception as e:
            print(f'  {inst}: ERROR {e}')


if __name__ == '__main__':
    main()
