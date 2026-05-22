"""research/mother_baby_v2_spot.py — Mother-Baby v2 strict test on NIFTY SPOT.

Fetches 15m SPOT (index) candles directly from Groww (CASH segment, NSE-NIFTY)
in 90-day chunks (API limit), caches them, then runs the strict single-baby
body-in-body v2 logic identical to mother_baby_v2_backtest.py.

Spot has no tradable instrument — this is a pattern-edge measurement only.

Run:  python research/mother_baby_v2_spot.py
"""
from __future__ import annotations

import sys
import pickle
import pathlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / 'v3' / 'cache' / 'candles_15m_spot_NIFTY.pkl'

MOTHER_MULT  = 1.2
MEDIAN_WIN   = 20
SLIPPAGE_PTS = 10.0
TARGET_MULT  = 1.0
MARGINS      = [0, 2, 5, 8, 12, 20]

START = date(2025, 1, 1)


def _fetch_15m_spot() -> pd.DataFrame:
    """Fetch 15m spot in 90-day chunks; cache to disk."""
    if CACHE.exists():
        df = pickle.load(open(CACHE, 'rb'))
        print(f"  cache hit: {len(df)} bars {df['ts'].min()} → {df['ts'].max()}")
        return df

    from v3.data.fetch_1m_spot_NIFTY import _get_groww
    g = _get_groww()
    frames = []
    cur = START
    today = date.today()
    while cur <= today:
        chunk_end = min(cur + timedelta(days=89), today)
        r = g.get_historical_candles(
            exchange='NSE', segment='CASH', groww_symbol='NSE-NIFTY',
            start_time=f"{cur} 09:15:00", end_time=f"{chunk_end} 15:30:00",
            candle_interval=g.CANDLE_INTERVAL_MIN_15,
        )
        c = r.get('candles', [])
        print(f"  {cur} → {chunk_end}: {len(c)} bars")
        if c:
            d = pd.DataFrame(c, columns=['ts', 'open', 'high', 'low', 'close',
                                         '_v', '_o'])
            frames.append(d[['ts', 'open', 'high', 'low', 'close']])
        cur = chunk_end + timedelta(days=1)

    df = pd.concat(frames, ignore_index=True)
    df['ts'] = pd.to_datetime(df['ts'])
    for c in ('open', 'high', 'low', 'close'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    pickle.dump(df, open(CACHE, 'wb'))
    print(f"  cached {len(df)} bars → {CACHE.name}")
    return df


def backtest(df: pd.DataFrame, margin: float, collect: bool = False) -> dict:
    df = df.copy()
    df['rng'] = df['high'] - df['low']
    df['med'] = df['rng'].rolling(MEDIAN_WIN, min_periods=5).median()
    trades = []

    for day, d in df.groupby(df['ts'].dt.date):
        d = d.reset_index(drop=True)
        n = len(d)
        i = 0
        while i < n - 2:
            mom = d.iloc[i]
            if pd.isna(mom['med']) or mom['rng'] < MOTHER_MULT * mom['med']:
                i += 1
                continue
            m_rng = mom['high'] - mom['low']
            m_body_hi = max(mom['open'], mom['close'])
            m_body_lo = min(mom['open'], mom['close'])

            baby = d.iloc[i + 1]
            b_body_hi = max(baby['open'], baby['close'])
            b_body_lo = min(baby['open'], baby['close'])
            if not (b_body_hi <= m_body_hi and b_body_lo >= m_body_lo):
                i += 1
                continue

            baby_hi, baby_lo = baby['high'], baby['low']
            trig = d.iloc[i + 2]

            entry = None
            long_lvl  = baby_hi + margin
            short_lvl = baby_lo - margin
            up_break   = trig['high'] >= long_lvl
            down_break = trig['low']  <= short_lvl
            if up_break and not down_break:
                entry = ('LONG', long_lvl)
            elif down_break and not up_break:
                entry = ('SHORT', short_lvl)
            if entry is None:
                i += 1
                continue

            side, epx = entry
            direction = 1 if side == 'LONG' else -1
            stop   = baby_lo if side == 'LONG' else baby_hi
            target = epx + direction * TARGET_MULT * m_rng
            ej = i + 2

            exit_px, exit_reason, exit_ts = None, None, None
            for k in range(ej + 1, n):
                bar = d.iloc[k]
                if side == 'LONG':
                    if bar['low'] <= stop:
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar['ts']; break
                    if bar['high'] >= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar['ts']; break
                else:
                    if bar['high'] >= stop:
                        exit_px, exit_reason, exit_ts = stop, 'STOP', bar['ts']; break
                    if bar['low'] <= target:
                        exit_px, exit_reason, exit_ts = target, 'TARGET', bar['ts']; break
            if exit_px is None:
                exit_px, exit_reason = d.iloc[-1]['close'], 'EOD'
                exit_ts = d.iloc[-1]['ts']

            gross = (exit_px - epx) * direction
            net   = gross - 2 * SLIPPAGE_PTS
            trades.append({
                'day': str(day), 'side': side, 'margin': margin,
                'entry_ts': str(d.iloc[ej]['ts']), 'exit_ts': str(exit_ts),
                'entry': round(epx, 1), 'exit': round(exit_px, 1),
                'stop': round(stop, 1), 'target': round(target, 1),
                'mother_ts': str(d.iloc[i]['ts']),
                'baby_ts': str(d.iloc[i + 1]['ts']),
                'mother_rng': round(m_rng, 1),
                'gross_pts': round(gross, 1), 'net_pts': round(net, 1),
                'reason': exit_reason, 'win': int(net > 0),
            })
            i = ej + 1

    if not trades:
        return {'margin': margin, 'n': 0}
    t = pd.DataFrame(trades)
    cum = t['net_pts'].cumsum().values
    dd  = float((np.maximum.accumulate(cum) - cum).max()) if len(cum) else 0.0
    wins, losses = t[t['net_pts'] > 0]['net_pts'], t[t['net_pts'] <= 0]['net_pts']
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else float('inf')
    return {
        'margin': margin, 'n': len(t),
        'wr': round(t['win'].mean() * 100, 1),
        'net_pts': round(t['net_pts'].sum(), 0),
        'gross_pts': round(t['gross_pts'].sum(), 0),
        'avg_net': round(t['net_pts'].mean(), 2),
        'avg_gross': round(t['gross_pts'].mean(), 2),
        'profit_factor': round(pf, 2),
        'max_dd_pts': round(dd, 0),
        'trades': t if collect else None,
    }


def main() -> None:
    print("Fetching 15m NIFTY SPOT candles ...")
    df = _fetch_15m_spot()
    df = df[df['ts'].dt.time.between(pd.Timestamp('09:15').time(),
                                     pd.Timestamp('15:30').time())]
    print(f"\nSPOT 15m: {df['ts'].min()} → {df['ts'].max()}  "
          f"({df['ts'].dt.date.nunique()} days, {len(df)} bars)\n")
    print("Mother-Baby v2 (strict, body-in-body) on SPOT 15m — margin sweep:\n")
    print(f"  {'margin':>7} {'n':>5} {'WR%':>6} {'net_pts':>9} {'gross':>8} "
          f"{'avg_net':>9} {'avg_gr':>8} {'PF':>6} {'maxDD':>8}")
    print('  ' + '-' * 72)
    best = None
    for mg in MARGINS:
        r = backtest(df, mg)
        if r['n'] == 0:
            print(f"  {mg:>7} {'—':>5}"); continue
        print(f"  {mg:>7} {r['n']:>5} {r['wr']:>6.1f} {r['net_pts']:>+9.0f} "
              f"{r['gross_pts']:>+8.0f} {r['avg_net']:>+9.2f} "
              f"{r['avg_gross']:>+8.2f} {r['profit_factor']:>6.2f} "
              f"{r['max_dd_pts']:>+8.0f}")
        if best is None or r['net_pts'] > best['net_pts']:
            best = r

    out = ROOT / 'trade_logs'
    out.mkdir(exist_ok=True)
    if best:
        full = backtest(df, best['margin'], collect=True)
        full['trades'].to_csv(out / 'mother_baby_v2_SPOT_15m.csv', index=False)
        print(f"\n  best margin = {best['margin']} → "
              f"trade_logs/mother_baby_v2_SPOT_15m.csv ({best['n']} trades)")


if __name__ == '__main__':
    main()
