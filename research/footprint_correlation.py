"""research/footprint_correlation.py — Join trade journal + footprint context.

For every closed trade in the system (v3 paper trades extracted from runner
logs + VP-Trail paper journal), compute the footprint feature snapshot at
the entry timestamp, and emit a joined CSV:

    trade_logs/footprint_features.csv
        inst, strategy, entry_ts, exit_ts, direction, entry, exit,
        pnl_pts, pnl_rs, win, exit_reason, days_held,
        + all footprint_at(...) fields

This is the dataset for the eventual footprint-veto regression. We need
~30+ paired observations before any statistical claim. Until then, this
just runs once a week to refresh the dataset.

Run:    python research/footprint_correlation.py
Output: trade_logs/footprint_features.csv
"""
from __future__ import annotations

import argparse
import pathlib
import re
from datetime import datetime

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys; sys.path.insert(0, str(ROOT))

from research.footprint_features import footprint_at
from research.dom_features       import dom_at

OUT = ROOT / 'trade_logs' / 'footprint_features.csv'
DOM_LOOKBACK_MIN = 5     # tighter window than footprint — DOM persistence
                         # only meaningful over short horizons


# ─── v3 trade extraction from runner logs ────────────────────────────────────
RE_ENTRY_NIFTY = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[PAPER\] ENTER (CE|PE) BUY  '
    r'strike=(\d+) @ ([\d.]+).*score=([-\d.]+)'
)
RE_ENTRY_BN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[PAPER\] ENTER BN (CE|PE) BUY  '
    r'strike=(\d+) @ ([\d.]+).*score=([-\d.]+)'
)
RE_EXIT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[EXIT (\w+)\] PAPER (?:BN )?(CE|PE) '
    r'strike=(\d+).*entry=([\d.]+).*exit=([\d.]+).*pnl=([-\d.]+) pts'
)


def _parse_v3_log(path: pathlib.Path, inst: str) -> list[dict]:
    """Extract entry+exit pairs from a runner log file."""
    if not path.exists():
        return []
    text = path.read_text(errors='ignore')
    entries, exits = [], []

    re_entry = RE_ENTRY_BN if inst == 'BANKNIFTY' else RE_ENTRY_NIFTY
    for m in re_entry.finditer(text):
        entries.append({
            'inst': inst, 'strategy': 'v3-options',
            'entry_ts': datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'),
            'side': m.group(2),
            'strike': int(m.group(3)),
            'entry': float(m.group(4)),
            'score': float(m.group(5)),
            'direction': 1 if m.group(2) == 'CE' else -1,
        })
    for m in RE_EXIT.finditer(text):
        exits.append({
            'inst': inst,
            'exit_ts': datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'),
            'exit_reason': m.group(2),
            'side': m.group(3),
            'strike': int(m.group(4)),
            'entry_ck': float(m.group(5)),
            'exit': float(m.group(6)),
            'pnl_pts': float(m.group(7)),
        })

    # Pair entries with exits by (strike, side, chronological)
    pairs = []
    used = set()
    for e in entries:
        for i, x in enumerate(exits):
            if i in used: continue
            if x['side'] == e['side'] and x['strike'] == e['strike'] \
                    and x['exit_ts'] >= e['entry_ts']:
                pairs.append({**e, **{
                    'exit_ts':     x['exit_ts'],
                    'exit':        x['exit'],
                    'exit_reason': x['exit_reason'],
                    'pnl_pts':     x['pnl_pts'],
                    'win':         1 if x['pnl_pts'] > 0 else 0,
                    'days_held':   (x['exit_ts'] - e['entry_ts']).days,
                    'pnl_rs':      x['pnl_pts'] * (65 if inst == 'NIFTY' else 30),
                }})
                used.add(i)
                break
    return pairs


# ─── VP-Trail trades from the paper journal ──────────────────────────────────
def _load_vp_trades() -> list[dict]:
    p = ROOT / 'trade_logs' / 'vp_paper_journal.csv'
    if not p.exists():
        return []
    df = pd.read_csv(p)
    out = []
    for _, r in df.iterrows():
        out.append({
            'inst':        r['inst'],
            'strategy':    'vp-trail',
            'entry_ts':    pd.to_datetime(r['entry_ts']).to_pydatetime(),
            'exit_ts':     pd.to_datetime(r['exit_ts']).to_pydatetime(),
            'direction':   1 if r['direction'] == 'LONG' else -1,
            'side':        'CE' if r['direction'] == 'LONG' else 'PE',
            'strike':      0,
            'entry':       float(r['entry']),
            'exit':        float(r['exit']),
            'exit_reason': r['exit_reason'],
            'pnl_pts':     float(r['pnl_pts']),
            'pnl_rs':      float(r['pnl_rs']),
            'win':         int(r['win']),
            'days_held':   int(r['days_held']),
            'score':       None,
        })
    return out


# ─── Join trades + footprint context ─────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--lookback-min', type=int, default=15,
                    help='footprint lookback window in minutes (default 15)')
    args = ap.parse_args()

    trades = []
    trades.extend(_parse_v3_log(
        ROOT / 'logs' / 'trade_bot' / 'runner_nifty.log',     'NIFTY'))
    trades.extend(_parse_v3_log(
        ROOT / 'logs' / 'trade_bot' / 'runner_banknifty.log', 'BANKNIFTY'))
    trades.extend(_load_vp_trades())

    print(f'  loaded {len(trades)} trades total')
    if not trades:
        print('  no trades found — nothing to join')
        return

    enriched = []
    n_fp_data, n_dom_data, n_skip_fp, n_skip_dom = 0, 0, 0, 0
    for t in trades:
        fp = footprint_at(t['inst'], t['entry_ts'], lookback_min=args.lookback_min)
        # Pass footprint's ltp_at_ts to dom_at so wall-distance is anchored to
        # the actual print price, not the at-ts DOM mid (slightly different).
        ltp = fp.get('ltp_at_ts')
        dm = dom_at(t['inst'], t['entry_ts'],
                    lookback_min=DOM_LOOKBACK_MIN, ltp=ltp)
        row = {**t}
        for k, v in fp.items():
            if k in ('inst', 'ts'):
                continue
            row[f'fp_{k}'] = v
        for k, v in dm.items():
            if k in ('inst', 'ts', 'lookback_min'):
                continue
            # dom_at already prefixes its keys with 'dom_' — keep as-is
            row[k] = v
        enriched.append(row)
        if fp.get('data_available'):  n_fp_data  += 1
        else:                          n_skip_fp  += 1
        if dm.get('dom_data_available'): n_dom_data += 1
        else:                            n_skip_dom += 1

    df = pd.DataFrame(enriched)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f'  wrote {len(df)} rows to {OUT}')
    print(f'  with footprint data: {n_fp_data}  / skipped (no tick CSV):  {n_skip_fp}')
    print(f'  with DOM data:       {n_dom_data}  / skipped (no depth CSV): {n_skip_dom}')

    if n_fp_data >= 5:
        print('\n=== preview — footprint features ===')
        cols = ['inst','strategy','entry_ts','direction','pnl_rs','win',
                'fp_cvd_direction','fp_imb_ratio','fp_n_stacked_imbalances',
                'fp_poc_dist_pts','fp_last_bar_abs_ratio']
        print(df[df['fp_data_available']][cols].head(20).to_string(index=False))

    if n_dom_data >= 5:
        print('\n=== preview — DOM features ===')
        cols = ['inst','strategy','entry_ts','direction','pnl_rs','win',
                'dom_imbalance','dom_mean_imbalance','dom_imbalance_trend',
                'dom_largest_wall_dist','dom_spread_pts','dom_max_refresh_count']
        print(df[df['dom_data_available']][cols].head(20).to_string(index=False))


if __name__ == '__main__':
    main()
