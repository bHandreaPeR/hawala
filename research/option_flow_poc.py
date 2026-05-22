"""research/option_flow_poc.py — Does option flow predict the next NIFTY move?

POC / research script. Runs over accumulated option_flow trace files
(v3/cache/option_flow_trace_<inst>_<YYYYMMDD>.ndjson) and measures whether
a composite of the flow features has forward-predictive content for spot.

THEORY
------
Two mechanisms could let option flow lead spot:
  1. Market-maker delta hedging — when a MM takes an option position they
     hedge in the underlying; that hedge flow mechanically pushes spot.
  2. Positioning exhaustion — when cumulative option flow (CVD) gets
     extremely one-sided, the move is crowded and tends to mean-revert.

Mechanism (2) is what this POC actually finds: per-snapshot net flow mostly
LAGS spot (~60s, confirmation), but the CUMULATIVE CVD extreme has a
CONTRARIAN forward signal — high CVD predicts spot fade over 5-10 min.

SIGNALS
-------
  trend      =  tanh(net_z_20m  / 2)      net flow vs 20-min baseline
  reversion  = -tanh(cvd_z_20m  / 2)      fade extreme cumulative flow
  composite  =  0.35*trend + 0.65*reversion

VERDICT GATE
------------
  IC > +0.10 at 5-min horizon on OUT-OF-SAMPLE days  → worth trading
  hit-rate > 55% cost-adjusted                       → worth trading
  Anything from a SINGLE day is NOT conclusive — autocorrelation in 5-sec
  data means ~3700 snapshots ≈ only ~50-80 independent observations.

Run:
    python research/option_flow_poc.py                  # all trace files
    python research/option_flow_poc.py --inst BANKNIFTY
    python research/option_flow_poc.py --oos-split 0.6  # in/out-of-sample
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _roll_z(x: np.ndarray, w: int) -> np.ndarray:
    n = len(x)
    z = np.full(n, np.nan)
    for i in range(w, n):
        win = x[i - w:i]
        sd = win.std()
        z[i] = (x[i] - win.mean()) / sd if sd > 0 else 0.0
    return z


def _fwd_ret(spot: np.ndarray, k: int) -> np.ndarray:
    n = len(spot)
    fr = np.full(n, np.nan)
    fr[:n - k] = np.log(spot[k:] / spot[:n - k])
    return fr


def _load_trace(inst: str) -> list[dict]:
    """Concatenate all daily trace files for an instrument, in date order."""
    files = sorted(glob.glob(str(ROOT / 'v3' / 'cache'
                                  / f'option_flow_trace_{inst}_*.ndjson')))
    rows: list[dict] = []
    for f in files:
        day = pathlib.Path(f).stem.split('_')[-1]
        for line in open(f):
            try:
                r = json.loads(line)
                r['_day'] = day
                rows.append(r)
            except Exception:
                continue
    return rows


def build_composite(net: np.ndarray, cvd: np.ndarray) -> dict:
    """Return dict of feature arrays + the composite signal."""
    trend     = np.tanh(_roll_z(net, 240) / 2.0)
    reversion = -np.tanh(_roll_z(cvd, 240) / 2.0)
    composite = 0.35 * trend + 0.65 * reversion
    return {'trend': trend, 'reversion': reversion, 'composite': composite}


def analyse(inst: str, oos_split: float | None) -> None:
    rows = _load_trace(inst)
    if len(rows) < 500:
        print(f"  {inst}: only {len(rows)} snapshots — need ≥500 for a POC. "
              f"Let the daemon accumulate more days.")
        return

    days = sorted(set(r['_day'] for r in rows))
    spot = np.array([r['spot'] for r in rows], float)
    net  = np.array([r['net']  for r in rows], float)
    cvd  = np.array([r['cvd']  for r in rows], float)
    n    = len(rows)

    feats = build_composite(net, cvd)
    composite = feats['composite']

    try:
        from scipy.stats import spearmanr
        def ic(a, b):
            m = ~(np.isnan(a) | np.isnan(b))
            return spearmanr(a[m], b[m])[0] if m.sum() > 50 else float('nan')
    except ImportError:
        def ic(a, b):
            m = ~(np.isnan(a) | np.isnan(b))
            return np.corrcoef(a[m], b[m])[0, 1] if m.sum() > 50 else float('nan')

    print(f"\n{'='*64}\n  {inst} — {n} snapshots across {len(days)} day(s): "
          f"{days[0]}–{days[-1]}\n{'='*64}")

    horizons = [(12, '+1min'), (36, '+3min'), (60, '+5min'), (120, '+10min')]

    # ── In-sample IC ──────────────────────────────────────────────────────
    print("  Composite IC vs forward return (IN-SAMPLE — optimistic):")
    for k, lbl in horizons:
        i = ic(composite, _fwd_ret(spot, k))
        flag = '  ← tradeable' if abs(i) > 0.10 else ''
        print(f"    {lbl:8s} IC={i:+.3f}{flag}")

    # ── Out-of-sample split (by day, not by row) ──────────────────────────
    if oos_split and len(days) >= 2:
        cut_day = days[int(len(days) * oos_split)]
        is_mask  = np.array([r['_day'] <  cut_day for r in rows])
        oos_mask = np.array([r['_day'] >= cut_day for r in rows])
        print(f"\n  OUT-OF-SAMPLE (fit on {days[0]}–, test from {cut_day}):")
        for k, lbl in horizons:
            fr = _fwd_ret(spot, k)
            i_oos = ic(composite[oos_mask], fr[oos_mask])
            print(f"    {lbl:8s} OOS IC={i_oos:+.3f}")
    elif oos_split:
        print(f"\n  OUT-OF-SAMPLE: need ≥2 distinct days — have {len(days)}. "
              f"Re-run after more sessions.")

    # ── Directional hit-rate @ +5min ──────────────────────────────────────
    fr5 = _fwd_ret(spot, 60)
    print("\n  Directional hit-rate @ +5min by |signal| gate:")
    for thr in (0.0, 0.2, 0.4, 0.6):
        m = ~(np.isnan(composite) | np.isnan(fr5)) & (np.abs(composite) >= thr)
        if m.sum() < 30:
            print(f"    |sig|>={thr}: n={m.sum()} (too few)"); continue
        correct = ((composite[m] > 0) & (fr5[m] > 0)) | \
                  ((composite[m] < 0) & (fr5[m] < 0))
        hr  = correct.mean() * 100
        ret = (np.sign(composite[m]) * fr5[m]).mean() * 100
        print(f"    |sig|>={thr}: n={m.sum():4d}  hit={hr:4.1f}%  "
              f"avg dir-ret={ret:+.3f}%")

    # ── Honesty footer ────────────────────────────────────────────────────
    print(f"\n  CAVEATS:")
    if len(days) == 1:
        print(f"    • ONE day only — IC here is in-sample + path-dependent. "
              f"NOT conclusive.")
    print(f"    • 5-sec snapshots are autocorrelated: {n} rows ≈ "
          f"~{n//60} independent ~5-min windows.")
    print(f"    • avg dir-ret must beat round-trip cost (~0.01-0.02% futures, "
          f"more for options) to be tradeable.")
    print(f"    • Composite weights (0.35/0.65) were chosen after seeing "
          f"Monday's IC — expect OOS IC lower.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--inst', default='NIFTY')
    ap.add_argument('--oos-split', type=float, default=0.6,
                    help='fraction of days used as in-sample (rest = OOS)')
    args = ap.parse_args()
    analyse(args.inst.upper(), args.oos_split)


if __name__ == '__main__':
    main()
