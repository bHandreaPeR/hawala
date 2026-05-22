"""research/mother_baby_backtest.py — Mother-Baby (inside-bar) backtest on NIFTY.

Pattern:
  Mother  — a wide-range candle (range ≥ MOTHER_MULT × rolling-median range).
  Baby    — a candle fully inside the Mother's high-low. Need ≥1 baby.
  Entry   — first later candle that CLOSES beyond the Mother's extreme:
              close > mother_high → LONG ;  close < mother_low → SHORT.
  Stop    — opposite Mother extreme.
  Target  — measured move: 1× Mother range from entry.
  Abandon — >MAX_BABIES inside without resolution, or EOD.
  Intraday only — square off at the day's last bar if still open.

Costs: SLIPPAGE_PTS per leg (NIFTY ≈ 10) → round-trip applied to net P&L.

Honest naive test — NO trend / volume filter. Measures the raw pattern edge.

Run:  python research/mother_baby_backtest.py
"""
from __future__ import annotations

import pickle
import pathlib
import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent

MOTHER_MULT   = 1.2     # mother range ≥ this × rolling-median range
MEDIAN_WIN    = 20      # bars for the rolling-median range baseline
MAX_BABIES    = 10      # abandon formation if it coils longer than this
SLIPPAGE_PTS  = 10.0    # per leg → 20 round-trip
TARGET_MULT   = 1.0     # measured move = TARGET_MULT × mother range


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


def backtest(df: pd.DataFrame, tf_label: str) -> dict:
    """Run the mother-baby scan day-by-day. Returns stats + trade list."""
    df = df.copy()
    df['rng'] = df['high'] - df['low']
    df['med'] = df['rng'].rolling(MEDIAN_WIN, min_periods=5).median()
    trades = []

    for day, d in df.groupby(df.index.date):
        d = d.reset_index()
        tcol = d.columns[0]          # the (datetime) index column after reset
        n = len(d)
        i = 0
        while i < n - 2:
            row = d.iloc[i]
            # Mother test
            if pd.isna(row['med']) or row['rng'] < MOTHER_MULT * row['med']:
                i += 1
                continue
            m_hi, m_lo = row['high'], row['low']
            m_rng = m_hi - m_lo

            # Count babies, find the breakout candle
            j = i + 1
            babies = 0
            entry = None
            while j < n:
                c = d.iloc[j]
                inside = (c['high'] <= m_hi) and (c['low'] >= m_lo)
                if inside:
                    babies += 1
                    if babies > MAX_BABIES:
                        break          # coiled too long — abandon
                    j += 1
                    continue
                # not a baby — is it a confirmed breakout close?
                if babies >= 1 and c['close'] > m_hi:
                    entry = ('LONG', j, c['close'])
                elif babies >= 1 and c['close'] < m_lo:
                    entry = ('SHORT', j, c['close'])
                break                   # formation resolved (or ambiguous)

            if entry is None:
                i = j + 1 if j > i else i + 1
                continue

            side, ej, epx = entry
            direction = 1 if side == 'LONG' else -1
            stop   = m_lo if side == 'LONG' else m_hi
            target = epx + direction * TARGET_MULT * m_rng

            entry_ts       = d.iloc[ej][tcol]
            mother_ts      = d.iloc[i][tcol]
            # babies are the candles between the mother and the breakout bar
            baby_ts_first  = d.iloc[i + 1][tcol]      if babies >= 1 else ''
            baby_ts_last   = d.iloc[ej - 1][tcol]     if babies >= 1 else ''

            # Walk forward to resolve
            exit_px, exit_reason, exit_ts = None, None, None
            for k in range(ej + 1, n):
                bar = d.iloc[k]
                if side == 'LONG':
                    if bar['low'] <= stop:                 # stop first (conservative)
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar[tcol]; break
                    if bar['high'] >= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar[tcol]; break
                else:
                    if bar['high'] >= stop:
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar[tcol]; break
                    if bar['low'] <= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar[tcol]; break
            if exit_px is None:                            # EOD square-off
                exit_px, exit_reason = d.iloc[-1]['close'], 'EOD'
                exit_ts = d.iloc[-1][tcol]

            gross = (exit_px - epx) * direction
            net   = gross - 2 * SLIPPAGE_PTS
            trades.append({
                'day': str(day), 'tf': tf_label, 'side': side,
                'entry_ts': str(entry_ts), 'exit_ts': str(exit_ts),
                'entry': round(epx, 1), 'exit': round(exit_px, 1),
                'stop': round(stop, 1), 'target': round(target, 1),
                'babies': babies, 'mother_rng': round(m_rng, 1),
                'mother_ts': str(mother_ts),
                'mother_high': round(m_hi, 1), 'mother_low': round(m_lo, 1),
                'baby_ts_first': str(baby_ts_first),
                'baby_ts_last':  str(baby_ts_last),
                'gross_pts': round(gross, 1), 'net_pts': round(net, 1),
                'reason': exit_reason, 'win': int(net > 0),
            })
            # resume scanning after the trade's entry bar
            i = ej + 1

    if not trades:
        return {'tf': tf_label, 'n': 0}
    t = pd.DataFrame(trades)
    wins  = t[t['net_pts'] > 0]['net_pts']
    losses = t[t['net_pts'] <= 0]['net_pts']
    cum = t['net_pts'].cumsum().values
    dd  = float((np.maximum.accumulate(cum) - cum).max()) if len(cum) else 0.0
    pf  = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float('inf')
    return {
        'tf': tf_label,
        'n': len(t),
        'wr': round(t['win'].mean() * 100, 1),
        'gross_pts': round(t['gross_pts'].sum(), 1),
        'net_pts': round(t['net_pts'].sum(), 1),
        'avg_net': round(t['net_pts'].mean(), 2),
        'avg_win': round(wins.mean(), 1) if len(wins) else 0,
        'avg_loss': round(losses.mean(), 1) if len(losses) else 0,
        'profit_factor': round(pf, 2),
        'max_dd_pts': round(dd, 1),
        'exit_mix': t['reason'].value_counts().to_dict(),
        'trades': t,
    }


def main() -> None:
    df1m = _load_1m()
    print(f"NIFTY 1m: {df1m.index.min()} → {df1m.index.max()}  "
          f"({df1m.index.normalize().nunique()} trading days)\n")

    frames = {
        '1m':  df1m.between_time('09:15', '15:30'),
        '5m':  _resample(df1m, '5min'),
        '15m': _resample(df1m, '15min'),
    }

    print(f"{'TF':>4} {'trades':>7} {'WR%':>6} {'net_pts':>9} {'avg':>8} "
          f"{'PF':>6} {'maxDD':>8}  exit-mix")
    print('-' * 78)
    results = {}
    for tf, fr in frames.items():
        r = backtest(fr, tf)
        results[tf] = r
        if r['n'] == 0:
            print(f"{tf:>4}  no trades"); continue
        print(f"{tf:>4} {r['n']:>7} {r['wr']:>6.1f} {r['net_pts']:>+9.0f} "
              f"{r['avg_net']:>+8.2f} {r['profit_factor']:>6.2f} "
              f"{r['max_dd_pts']:>+8.0f}  {r['exit_mix']}")

    # Detail: per-TF win/loss anatomy
    print()
    for tf, r in results.items():
        if r['n'] == 0:
            continue
        print(f"  {tf}: avg_win={r['avg_win']:+.0f}  avg_loss={r['avg_loss']:+.0f}  "
              f"gross={r['gross_pts']:+.0f}  slippage_drag="
              f"{r['gross_pts']-r['net_pts']:+.0f} pts over {r['n']} trades")

    # Save trade logs
    out = ROOT / 'trade_logs'
    out.mkdir(exist_ok=True)
    for tf, r in results.items():
        if r['n']:
            r['trades'].to_csv(out / f'mother_baby_NIFTY_{tf}.csv', index=False)
    print(f"\n  trade logs → trade_logs/mother_baby_NIFTY_<tf>.csv")


if __name__ == '__main__':
    main()
