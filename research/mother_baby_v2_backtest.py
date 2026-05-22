"""research/mother_baby_v2_backtest.py — Strict single-baby inside-bar variant.

Modified pattern (per spec):
  Mother  — wide-range candle (range ≥ MOTHER_MULT × rolling-median range).
  Baby    — EXACTLY ONE candle, the one right after the Mother, whose BODY
            (min(O,C) … max(O,C)) sits inside the Mother's BODY.
  Trigger — the very next candle (Mother+2) must break the BABY's high/low
            by MARGIN points:
              high ≥ baby_high + MARGIN → LONG  entry at baby_high + MARGIN
              low  ≤ baby_low  − MARGIN → SHORT entry at baby_low  − MARGIN
            If it breaks neither → no trade (strict — only this one candle).
  Stop    — baby's opposite extreme (LONG → baby_low ; SHORT → baby_high).
  Target  — measured move: 1× Mother range from entry.
  Intraday only — EOD square-off.

Sweeps MARGIN to find what filter level (if any) makes it viable.

Costs: SLIPPAGE_PTS per leg → round-trip on net P&L.

Run:  python research/mother_baby_v2_backtest.py
"""
from __future__ import annotations

import pickle
import pathlib
import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent

MOTHER_MULT  = 1.2
MEDIAN_WIN   = 20
SLIPPAGE_PTS = 10.0
TARGET_MULT  = 1.0
MARGINS      = [0, 2, 5, 8, 12, 20]   # points — the break-confirmation buffer


def _load_1m() -> pd.DataFrame:
    df = pickle.load(open(ROOT / 'v3' / 'cache' / 'candles_1m_NIFTY.pkl', 'rb'))
    df['ts'] = pd.to_datetime(df['ts'])
    return df.set_index('ts')[['open', 'high', 'low', 'close']].sort_index()


def _resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    g = df1m.resample(rule, label='right', closed='right')
    out = pd.DataFrame({
        'open':  g['open'].first(), 'high': g['high'].max(),
        'low':   g['low'].min(),    'close': g['close'].last(),
    }).dropna()
    return out.between_time('09:15', '15:30')


def backtest(df: pd.DataFrame, margin: float, tf_label: str,
             collect: bool = False) -> dict:
    df = df.copy()
    df['rng'] = df['high'] - df['low']
    df['med'] = df['rng'].rolling(MEDIAN_WIN, min_periods=5).median()
    trades = []

    for day, d in df.groupby(df.index.date):
        d = d.reset_index()
        tcol = d.columns[0]
        n = len(d)
        i = 0
        while i < n - 2:
            mom = d.iloc[i]
            # Mother test — wide range
            if pd.isna(mom['med']) or mom['rng'] < MOTHER_MULT * mom['med']:
                i += 1
                continue
            m_rng = mom['high'] - mom['low']
            m_body_hi = max(mom['open'], mom['close'])
            m_body_lo = min(mom['open'], mom['close'])

            baby = d.iloc[i + 1]
            b_body_hi = max(baby['open'], baby['close'])
            b_body_lo = min(baby['open'], baby['close'])
            # Baby test — BODY inside Mother BODY
            if not (b_body_hi <= m_body_hi and b_body_lo >= m_body_lo):
                i += 1
                continue

            baby_hi, baby_lo = baby['high'], baby['low']
            trig = d.iloc[i + 2]                       # the one subsequent candle

            entry = None
            long_lvl  = baby_hi + margin
            short_lvl = baby_lo - margin
            up_break   = trig['high'] >= long_lvl
            down_break = trig['low']  <= short_lvl
            if up_break and not down_break:
                entry = ('LONG', long_lvl)
            elif down_break and not up_break:
                entry = ('SHORT', short_lvl)
            elif up_break and down_break:
                entry = None                          # ambiguous — skip
            if entry is None:
                i += 1
                continue

            side, epx = entry
            direction = 1 if side == 'LONG' else -1
            stop   = baby_lo if side == 'LONG' else baby_hi
            target = epx + direction * TARGET_MULT * m_rng
            ej = i + 2                                # trigger bar index

            exit_px, exit_reason, exit_ts = None, None, None
            for k in range(ej + 1, n):
                bar = d.iloc[k]
                if side == 'LONG':
                    if bar['low'] <= stop:
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar[tcol]; break
                    if bar['high'] >= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar[tcol]; break
                else:
                    if bar['high'] >= stop:
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar[tcol]; break
                    if bar['low'] <= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar[tcol]; break
            if exit_px is None:
                exit_px, exit_reason = d.iloc[-1]['close'], 'EOD'
                exit_ts = d.iloc[-1][tcol]

            gross = (exit_px - epx) * direction
            net   = gross - 2 * SLIPPAGE_PTS
            rec = {
                'day': str(day), 'tf': tf_label, 'side': side, 'margin': margin,
                'entry_ts': str(d.iloc[ej][tcol]), 'exit_ts': str(exit_ts),
                'entry': round(epx, 1), 'exit': round(exit_px, 1),
                'stop': round(stop, 1), 'target': round(target, 1),
                'mother_ts': str(d.iloc[i][tcol]),
                'mother_high': round(mom['high'], 1), 'mother_low': round(mom['low'], 1),
                'baby_ts': str(d.iloc[i + 1][tcol]),
                'baby_high': round(baby_hi, 1), 'baby_low': round(baby_lo, 1),
                'mother_rng': round(m_rng, 1),
                'gross_pts': round(gross, 1), 'net_pts': round(net, 1),
                'reason': exit_reason, 'win': int(net > 0),
            }
            trades.append(rec)
            i = ej + 1

    if not trades:
        return {'tf': tf_label, 'margin': margin, 'n': 0}
    t = pd.DataFrame(trades)
    cum = t['net_pts'].cumsum().values
    dd  = float((np.maximum.accumulate(cum) - cum).max()) if len(cum) else 0.0
    wins, losses = t[t['net_pts'] > 0]['net_pts'], t[t['net_pts'] <= 0]['net_pts']
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else float('inf')
    return {
        'tf': tf_label, 'margin': margin, 'n': len(t),
        'wr': round(t['win'].mean() * 100, 1),
        'net_pts': round(t['net_pts'].sum(), 0),
        'gross_pts': round(t['gross_pts'].sum(), 0),
        'avg_net': round(t['net_pts'].mean(), 2),
        'profit_factor': round(pf, 2),
        'max_dd_pts': round(dd, 0),
        'trades': t if collect else None,
    }


def main() -> None:
    df1m = _load_1m()
    print(f"NIFTY 1m: {df1m.index.min()} → {df1m.index.max()}\n")
    frames = {
        '1m':  df1m.between_time('09:15', '15:30'),
        '5m':  _resample(df1m, '5min'),
        '15m': _resample(df1m, '15min'),
    }
    print("Strict single-baby (body-in-body) + baby-break with margin sweep:\n")
    print(f"  {'TF':>4} {'margin':>7} {'n':>5} {'WR%':>6} {'net_pts':>9} "
          f"{'gross':>8} {'avg':>8} {'PF':>6} {'maxDD':>8}")
    print('  ' + '-' * 70)
    best = {}
    for tf, fr in frames.items():
        for mg in MARGINS:
            r = backtest(fr, mg, tf)
            if r['n'] == 0:
                print(f"  {tf:>4} {mg:>7} {'—':>5}"); continue
            print(f"  {tf:>4} {mg:>7} {r['n']:>5} {r['wr']:>6.1f} "
                  f"{r['net_pts']:>+9.0f} {r['gross_pts']:>+8.0f} "
                  f"{r['avg_net']:>+8.2f} {r['profit_factor']:>6.2f} "
                  f"{r['max_dd_pts']:>+8.0f}")
            # track best net per TF
            if tf not in best or r['net_pts'] > best[tf]['net_pts']:
                best[tf] = r
        print()

    # Save the best-margin 15m trade log for the explorer
    out = ROOT / 'trade_logs'
    out.mkdir(exist_ok=True)
    if '15m' in best:
        bm = best['15m']['margin']
        full = backtest(frames['15m'], bm, '15m', collect=True)
        if full.get('trades') is not None:
            full['trades'].to_csv(out / 'mother_baby_v2_NIFTY_15m.csv', index=False)
            print(f"  best 15m margin = {bm} pts → "
                  f"trade_logs/mother_baby_v2_NIFTY_15m.csv ({full['n']} trades)")


if __name__ == '__main__':
    main()
