"""
research/trade_explorer.py — Build a single HTML trade explorer.

For every trade across BANKNIFTY / NIFTY / SENSEX (IS + OOS), pre-compute
the chart context (15m candles within the contract, full + sub volume
profiles, time-evolving VAH/VAL/POC tracks, regime-shift markers, and the
trade entry/exit) and embed all of it in one Plotly-powered HTML page.

Drop-down picks a trade; Plotly.react swaps in that trade's data without
reloading the page.

Output: research/trade_explorer.html  (self-contained, ~5-15 MB)

Run:
    python -m research.trade_explorer
    python -m research.trade_explorer --filter OOS
    python -m research.trade_explorer --filter NIFTY
"""

from __future__ import annotations

import argparse
import json
import pathlib
import pickle
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import INSTRUMENTS                                  # noqa: E402
from strategies.volume_profile import (                         # noqa: E402
    _bar_distribute_volume,
    _value_area,
    detect_regime_shifts,
    _build_daily_summary,
    _is_regime_shift,
)

CACHE_DIR = ROOT / 'data' / 'cache_15m'
TRADE_DIR = ROOT / 'trade_logs'

VA_PCT  = 0.70
INSTRUMENT_BINS = {'BANKNIFTY': 20, 'NIFTY': 5, 'SENSEX': 25}
SNAPSHOT_BARS   = 4   # take VA snapshot every N bars (4 × 15m = 1 hour)


# ── Cache loading ─────────────────────────────────────────────────────────────

def _load_combined(instrument: str) -> pd.DataFrame:
    """Load 15m candles for an instrument.

    Primary: data/cache_15m/<inst>_combined.pkl (v2 cache, has Contract/Expiry).
    Fallback: resample v3/cache/candles_1m_<inst>.pkl → 15m when the v2
    combined cache is absent (main repo doesn't ship it)."""
    f = CACHE_DIR / f'{instrument}_combined.pkl'
    if f.exists():
        with open(f, 'rb') as h:
            df = pickle.load(h)
        df = df.rename(columns={
            'open': 'Open', 'high': 'High', 'low': 'Low',
            'close': 'Close', 'volume': 'Volume', 'oi': 'Oi',
            'contract': 'Contract', 'expiry': 'Expiry',
        })
        return df.between_time('09:15', '15:30')

    # Fallback — v3 1m cache resampled to 15m
    v3f = ROOT / 'v3' / 'cache' / f'candles_1m_{instrument}.pkl'
    if not v3f.exists():
        return pd.DataFrame()
    raw = pickle.load(open(v3f, 'rb'))
    raw['ts'] = pd.to_datetime(raw['ts'])
    raw = raw.set_index('ts').sort_index()
    g = raw.resample('15min', label='right', closed='right')
    df = pd.DataFrame({
        'Open':  g['open'].first(),  'High': g['high'].max(),
        'Low':   g['low'].min(),     'Close': g['close'].last(),
        'Volume': g['volume'].sum(),
    }).dropna(subset=['Open'])
    df['Contract'] = f'{instrument}-FUT'   # synthetic — fallback has no roll info
    df['Expiry']   = pd.NaT
    return df.between_time('09:15', '15:30')


def _load_trades(instrument: str, prefix: str = 'vpt') -> pd.DataFrame:
    parts = []
    for tag in ('IS', 'OOS'):
        f = TRADE_DIR / f'{prefix}_{instrument}_{tag}.csv'
        if f.exists():
            t = pd.read_csv(f)
            t['period'] = tag
            t['instrument_name'] = instrument
            parts.append(t)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    # Strategy emits entry_ts / exit_ts; downstream code expects entry_time / exit_time.
    if 'entry_ts' in out.columns and 'entry_time' not in out.columns:
        out = out.rename(columns={'entry_ts': 'entry_time',
                                   'exit_ts':  'exit_time'})
    for col in ('entry_time', 'exit_time'):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors='coerce')
    out['date'] = pd.to_datetime(out['date']).dt.date
    return out


def _load_full_backtest_trades() -> pd.DataFrame:
    """ORB / VWAP_REV / OPT_ORB live in full_backtest_*.csv."""
    parts = []
    for f in sorted((TRADE_DIR).glob('full_backtest_*.csv')):
        parts.append(pd.read_csv(f))
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True).drop_duplicates(
        ['trade_id', 'strategy', 'date', 'entry_time'], keep='last')
    out['entry_time'] = pd.to_datetime(out['entry_time'], errors='coerce')
    out['exit_time']  = pd.to_datetime(out['exit_time'],  errors='coerce')
    out['date']       = pd.to_datetime(out['date']).dt.date
    # Map NSE/BSE instrument names to the cache keys
    name_map = {'NSE-BANKNIFTY': 'BANKNIFTY', 'NSE-NIFTY': 'NIFTY',
                'BSE-SENSEX': 'SENSEX'}
    out['instrument_name'] = out['instrument'].map(name_map).fillna(out['instrument'])
    out['period'] = out.apply(
        lambda r: 'OOS' if pd.Timestamp(r['date']) >= pd.Timestamp('2026-01-01')
                       else 'IS', axis=1)
    return out


def _load_mother_baby_trades(tf: str = '15m') -> pd.DataFrame:
    """Load mother-baby inside-bar backtest trades for a timeframe (NIFTY only).
    Prefers the v2 strict single-baby file; falls back to v1 if absent."""
    f_v2 = TRADE_DIR / f'mother_baby_v2_NIFTY_{tf}.csv'
    f_v1 = TRADE_DIR / f'mother_baby_NIFTY_{tf}.csv'
    f = f_v2 if f_v2.exists() else f_v1
    if not f.exists():
        return pd.DataFrame()
    d = pd.read_csv(f)
    # v2 has a single `baby_ts` (one baby); v1 has baby_ts_first/last.
    # Normalise so the overlay code path works for both.
    if 'baby_ts' in d.columns and 'baby_ts_first' not in d.columns:
        d['baby_ts_first'] = d['baby_ts']
        d['baby_ts_last']  = d['baby_ts']
        d['babies']        = 1
    d['strategy']        = f'MOTHER_BABY_{tf}'
    d['instrument_name'] = 'NIFTY'
    d['entry_time']      = pd.to_datetime(d['entry_ts'], errors='coerce')
    d['exit_time']       = pd.to_datetime(d['exit_ts'],  errors='coerce')
    d['date']            = pd.to_datetime(d['day']).dt.date
    d['direction']       = d['side']                    # LONG / SHORT
    d['exit_price']      = d['exit']
    d['exit_reason']     = d['reason']
    d['pnl_pts']         = d['net_pts']
    d['pnl_rs']          = (d['net_pts'] * 75).round(0)  # NIFTY lot 75
    # entry/stop/target columns already named entry/stop/target
    d['period'] = d['entry_time'].apply(
        lambda t: 'OOS' if pd.notna(t) and t >= pd.Timestamp('2026-01-01') else 'IS')
    return d


def _load_credit_spread_trades() -> pd.DataFrame:
    parts = []
    for f in sorted((TRADE_DIR).glob('spr_swing_ext_*.csv')):
        d = pd.read_csv(f)
        d['period'] = 'IS' if 'IS.csv' in f.name else 'OOS'
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out['strategy']   = 'CREDIT_SPREAD'
    out['entry_time'] = pd.to_datetime(out['entry_ts'], errors='coerce')
    out['exit_time']  = pd.to_datetime(out['exit_ts'],  errors='coerce')
    out['date']       = pd.to_datetime(out['date']).dt.date
    name_map = {'NSE-BANKNIFTY': 'BANKNIFTY', 'NSE-NIFTY': 'NIFTY',
                'BSE-SENSEX': 'SENSEX'}
    out['instrument_name'] = out['instrument'].map(name_map).fillna(out['instrument'])
    return out


def _build_simple_dataset(trade: pd.Series, all_data: pd.DataFrame,
                          strategy: str) -> dict | None:
    """Lightweight dataset for non-VP strategies: candles + trade markers,
    no profile / value-area context."""
    instrument = trade['instrument_name']
    et = pd.Timestamp(trade.get('entry_time'))
    xt = pd.Timestamp(trade.get('exit_time')) if pd.notna(trade.get('exit_time')) else et

    # 5-day window centred on the trade — enough context, small payload
    win_start = (et - pd.Timedelta(days=2)).normalize()
    win_end   = (xt + pd.Timedelta(days=2)).normalize() + pd.Timedelta(hours=23)
    window = all_data[(all_data.index >= win_start) & (all_data.index <= win_end)]
    if window.empty:
        return None

    candles_ts, o_arr, h_arr, l_arr, c_arr, v_arr = [], [], [], [], [], []
    for ts, br in window.iterrows():
        candles_ts.append(ts.isoformat())
        o_arr.append(float(br['Open']));  h_arr.append(float(br['High']))
        l_arr.append(float(br['Low']));   c_arr.append(float(br['Close']))
        v_arr.append(float(br.get('Volume', 0)))

    pnl = float(trade.get('pnl_rs', 0))
    label = (f"{strategy} · {instrument} · {trade.get('period','')} · "
             f"{et:%Y-%m-%d %H:%M} · "
             f"{trade.get('direction','?')} @ "
             f"{float(trade.get('entry',0)):,.0f} → "
             f"{trade.get('exit_reason','?')}  ₹{pnl:+,.0f}")

    return {
        'id':         int(trade.name),
        'strategy':   strategy,
        'label':      label,
        'instrument': instrument,
        'contract':   str(trade.get('contract', '')),
        'bin_pts':    INSTRUMENT_BINS.get(instrument, 20),
        'has_options': strategy in ('OPT_ORB', 'CREDIT_SPREAD'),
        'candles':  {'ts': candles_ts, 'o': o_arr, 'h': h_arr,
                     'l': l_arr, 'c': c_arr, 'v': v_arr},
        # Empty profile fields so the front-end can render uniformly
        'full_profile': {'bins': [], 'vols': []},
        'sub_profile':  None,
        'va_track_full': [],
        'va_track_sub':  [],
        'final_va_full': {'vah': None, 'val': None, 'poc': None},
        'final_va_sub':  None,
        'regime_shifts': [],
        'regime_start':  None,
        'trade': {
            'entry_ts':    et.isoformat() if pd.notna(et) else None,
            'exit_ts':     xt.isoformat() if pd.notna(xt) else None,
            'entry_px':    float(trade.get('entry', 0)),
            'exit_px':     float(trade.get('exit_price', 0)),
            'stop':        float(trade.get('stop', 0) or 0),
            'target':      float(trade.get('target', 0) or 0),
            'direction':   str(trade.get('direction', '')),
            'exit_reason': str(trade.get('exit_reason', '')),
            'pnl_pts':     float(trade.get('pnl_pts', 0) or 0),
            'pnl_rs':      pnl,
            'profile_used': '—',
        },
        # Mother-Baby overlay — present only when the trade row carries the
        # mother/baby candle positions (mother_baby_NIFTY_*.csv).
        'mother_baby': _mother_baby_overlay(trade),
    }


def _mother_baby_overlay(trade: pd.Series) -> dict | None:
    """Extract mother/baby candle positions for the explorer overlay.
    Returns None if the trade row has no mother_ts (non-MB strategy)."""
    m_ts = trade.get('mother_ts')
    if m_ts is None or (isinstance(m_ts, float) and pd.isna(m_ts)) or str(m_ts) in ('', 'nan', 'NaT'):
        return None
    def _iso(v):
        try:
            t = pd.Timestamp(v)
            return t.isoformat() if pd.notna(t) else None
        except Exception:
            return None
    return {
        'mother_ts':   _iso(m_ts),
        'mother_high': float(trade.get('mother_high', 0) or 0),
        'mother_low':  float(trade.get('mother_low', 0) or 0),
        'baby_first':  _iso(trade.get('baby_ts_first')),
        'baby_last':   _iso(trade.get('baby_ts_last')),
        'n_babies':    int(trade.get('babies', 0) or 0),
        # v2: the baby's high/low ARE the breakout reference levels
        'baby_high':   float(trade.get('baby_high', 0) or 0) or None,
        'baby_low':    float(trade.get('baby_low', 0) or 0) or None,
    }


# ── Per-trade dataset ─────────────────────────────────────────────────────────

def _build_trade_dataset(trade: pd.Series, all_data: pd.DataFrame,
                         daily: pd.DataFrame) -> dict | None:
    """
    Given one trade row + the full instrument cache, build the JSON-able
    dict the front-end needs to render the chart.
    """
    instrument = trade['instrument_name']
    bin_pts    = INSTRUMENT_BINS[instrument]

    # Strategy doesn't emit `contract` per row, so resolve it from the
    # cache: whichever Contract is active on the trade's date.
    trade_day_data = all_data[all_data.index.date == trade['date']]
    if trade_day_data.empty or 'Contract' not in trade_day_data.columns:
        return None
    contract = str(trade_day_data['Contract'].iloc[0])
    contract_data = all_data[all_data['Contract'] == contract]
    if contract_data.empty:
        return None

    # Show the FULL contract — from the first bar through the contract's
    # expiry day. Lets the trader see how the auction played out after
    # the trade exit and how the value area finalised.
    window = contract_data.copy()
    if window.empty:
        return None

    # Walk window bar-by-bar; build profile_full and (if applicable) profile_sub.
    profile_full: dict = {}
    profile_sub:  dict = {}
    regime_start_iso: str | None = None
    if 'regime_start' in trade and pd.notna(trade['regime_start']):
        try:
            regime_start_iso = (pd.Timestamp(trade['regime_start']).date()
                                .isoformat())
        except Exception:
            regime_start_iso = None

    # Determine regime-shift dates that occurred within the window
    # (so we can mark them on the chart, not just the one used by this trade)
    win_dates = sorted(set(window.index.date))
    detected_shifts: list = []
    prev_full_poc: float | None = None
    profile_full_walk: dict = {}
    for d in win_dates:
        if _is_regime_shift(daily, d, prev_full_poc,
                            gap_atr=1.5, vol_mult=1.5, accept_atr=1.0):
            detected_shifts.append(d.isoformat())
        day_df = window[window.index.date == d]
        for _, br in day_df.iterrows():
            _bar_distribute_volume(profile_full_walk,
                                   float(br['Low']), float(br['High']),
                                   float(br.get('Volume', 0)), bin_pts)
        _, _, prev_full_poc, _ = _value_area(profile_full_walk, bin_pts, VA_PCT)

    # Profiles + VA tracks (snapshots every SNAPSHOT_BARS bars)
    va_track_full: list = []
    va_track_sub:  list = []
    in_sub = False
    sub_start_dt = None
    if regime_start_iso is not None:
        sub_start_dt = pd.Timestamp(regime_start_iso)

    candles_ts: list = []; o_arr=[]; h_arr=[]; l_arr=[]; c_arr=[]; v_arr=[]
    bar_idx = 0
    for ts, br in window.iterrows():
        l = float(br['Low']); h = float(br['High'])
        c = float(br['Close']); o = float(br['Open'])
        v = float(br.get('Volume', 0))
        candles_ts.append(ts.isoformat())
        o_arr.append(o); h_arr.append(h); l_arr.append(l)
        c_arr.append(c); v_arr.append(v)

        _bar_distribute_volume(profile_full, l, h, v, bin_pts)
        if sub_start_dt is not None and ts >= sub_start_dt:
            _bar_distribute_volume(profile_sub, l, h, v, bin_pts)

        if bar_idx % SNAPSHOT_BARS == 0:
            vah, val, poc, _ = _value_area(profile_full, bin_pts, VA_PCT)
            if vah is not None:
                va_track_full.append({
                    'ts':  ts.isoformat(),
                    'vah': round(vah, 2), 'val': round(val, 2),
                    'poc': round(poc, 2),
                })
            if profile_sub:
                vah_s, val_s, poc_s, _ = _value_area(profile_sub, bin_pts, VA_PCT)
                if vah_s is not None:
                    va_track_sub.append({
                        'ts':  ts.isoformat(),
                        'vah': round(vah_s, 2), 'val': round(val_s, 2),
                        'poc': round(poc_s, 2),
                    })
        bar_idx += 1

    vah_f, val_f, poc_f, _ = _value_area(profile_full, bin_pts, VA_PCT)
    sub_payload = None
    final_va_sub = None
    if profile_sub:
        bins_s = sorted(profile_sub.keys())
        sub_payload = {
            'bins': [round(b, 2) for b in bins_s],
            'vols': [round(profile_sub[b], 2) for b in bins_s],
        }
        vah_s, val_s, poc_s, _ = _value_area(profile_sub, bin_pts, VA_PCT)
        final_va_sub = {'vah': round(vah_s, 2),
                        'val': round(val_s, 2),
                        'poc': round(poc_s, 2)} if vah_s is not None else None

    bins_f = sorted(profile_full.keys())

    # Detect options-overlay log: presence of opt_* fields
    has_options = pd.notna(trade.get('opt_strike', None))
    if has_options:
        kind  = str(trade.get('opt_kind', 'C'))
        cp    = 'CE' if kind == 'C' else 'PE'
        K     = int(trade.get('opt_strike', 0))
        opt_pnl = float(trade.get('opt_pnl_rs', 0))
        prem_in = float(trade.get('opt_premium_in', 0))
        prem_out = float(trade.get('opt_premium_out', 0))
        budget   = float(trade.get('opt_budget_rs', 0))
        ret_pct  = float(trade.get('opt_return_pct', 0))
        dte      = int(trade.get('opt_dte_entry', 0))
        expiry   = str(trade.get('opt_expiry', ''))
        label = (f"{instrument} · {trade.get('period','')} · "
                 f"{trade['entry_time'].strftime('%Y-%m-%d %H:%M') if pd.notna(trade.get('entry_time')) else trade['date']} · "
                 f"BUY {K:,} {cp} ({dte}d) @ ₹{prem_in:.0f} → "
                 f"₹{prem_out:.0f}  ₹{opt_pnl:+,.0f} ({ret_pct:+.0f}%)")
    else:
        label_pnl = trade.get('pnl_rs', 0)
        label = (f"{instrument} · {trade.get('period','')} · "
                 f"{trade['entry_time'].strftime('%Y-%m-%d %H:%M') if pd.notna(trade.get('entry_time')) else trade['date']} · "
                 f"{trade.get('direction','?'):>5} @ {trade.get('entry','?'):,.0f} → "
                 f"{trade.get('exit_reason','?')} ₹{label_pnl:+,.0f} · "
                 f"({trade.get('profile_used','?')})")

    trade_dict = {
        'entry_ts': (trade['entry_time'].isoformat()
                     if pd.notna(trade.get('entry_time')) else None),
        'exit_ts':  (trade['exit_time'].isoformat()
                     if pd.notna(trade.get('exit_time')) else None),
        'entry_px': float(trade.get('entry', 0)),
        'exit_px':  float(trade.get('exit_price', 0)),
        'stop':     float(trade.get('stop', 0)),
        'target':   float(trade.get('target', 0)),
        'direction': str(trade.get('direction', '')),
        'exit_reason': str(trade.get('exit_reason', '')),
        'pnl_pts':  float(trade.get('pnl_pts', 0)),
        'pnl_rs':   float(trade.get('pnl_rs', 0)),
        'profile_used': str(trade.get('profile_used', '')),
    }
    if has_options:
        trade_dict['opt'] = {
            'strike':       int(trade.get('opt_strike', 0)),
            'kind':         str(trade.get('opt_kind', 'C')),
            'expiry':       str(trade.get('opt_expiry', '')),
            'dte_entry':    int(trade.get('opt_dte_entry', 0)),
            'dte_exit':     int(trade.get('opt_dte_exit', 0)),
            'premium_in':   float(trade.get('opt_premium_in', 0)),
            'premium_out':  float(trade.get('opt_premium_out', 0)),
            'pnl_pts':      float(trade.get('opt_pnl_pts', 0)),
            'pnl_rs':       float(trade.get('opt_pnl_rs', 0)),
            'budget_rs':    float(trade.get('opt_budget_rs', 0)),
            'return_pct':   float(trade.get('opt_return_pct', 0)),
        }

    return {
        'id':       int(trade.name),
        'strategy': 'VP_TRAIL',
        'label':    label,
        'instrument': instrument,
        'contract': contract,
        'bin_pts':  bin_pts,
        'has_options': bool(has_options),
        'candles':  {'ts': candles_ts, 'o': o_arr, 'h': h_arr,
                     'l': l_arr, 'c': c_arr, 'v': v_arr},
        'full_profile': {'bins': [round(b, 2) for b in bins_f],
                         'vols': [round(profile_full[b], 2) for b in bins_f]},
        'sub_profile':  sub_payload,
        'va_track_full': va_track_full,
        'va_track_sub':  va_track_sub,
        'final_va_full': {'vah': round(vah_f, 2) if vah_f is not None else None,
                          'val': round(val_f, 2) if val_f is not None else None,
                          'poc': round(poc_f, 2) if poc_f is not None else None},
        'final_va_sub':  final_va_sub,
        'regime_shifts': detected_shifts,
        'regime_start':  regime_start_iso,
        'trade': trade_dict,
    }


# ── HTML emission ─────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Volume-Profile Trade Explorer (Options)</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
         margin: 0; padding: 16px; background: #fafafa; color: #111; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  .controls { display: flex; gap: 12px; align-items: center; margin-bottom: 12px;
              flex-wrap: wrap; }
  select { font-size: 13px; padding: 6px 10px; min-width: 720px;
           border: 1px solid #ccc; border-radius: 4px; background: white; }
  .meta { font-size: 12px; color: #555; padding: 6px 10px; background: white;
          border: 1px solid #e5e7eb; border-radius: 4px; flex: 1; }
  #chart { width: 100%; height: 720px; background: white;
           border: 1px solid #e5e7eb; border-radius: 4px; }
  button { font-size: 12px; padding: 4px 10px; border-radius: 4px;
           border: 1px solid #ccc; background: white; cursor: pointer; }
  button:hover { background: #f3f4f6; }
  .chip { display: inline-block; padding: 4px 10px; margin-right: 6px;
          font-size: 12px; border: 1px solid #ccc; border-radius: 14px;
          background: white; cursor: pointer; user-select: none; }
  .chip.on { background: #1F4E79; color: white; border-color: #1F4E79; }
  .chip:hover:not(.on) { background: #eef2ff; }
  .chips { margin-bottom: 8px; }
</style>
</head>
<body>
<h1>Hawala Trade Explorer · <span id="trade-count">${TRADE_COUNT}</span> trades</h1>
<div class="chips" id="strat-chips"></div>
<div class="controls">
  <select id="picker" onchange="render(parseInt(this.value))"></select>
  <button onclick="step(-1)">◀ prev</button>
  <button onclick="step(1)">next ▶</button>
  <span class="meta" id="meta"></span>
</div>
<div id="chart"></div>

<script>
const TRADES = ${TRADES_JSON};

// Strategy filter state — all on by default
const ALL_STRATS = Array.from(new Set(TRADES.map(t => t.strategy || 'VP_TRAIL')));
const stratOn = Object.fromEntries(ALL_STRATS.map(s => [s, true]));

function buildChips() {
  const host = document.getElementById('strat-chips');
  host.innerHTML = '';
  ALL_STRATS.forEach(s => {
    const n = TRADES.filter(t => (t.strategy||'VP_TRAIL') === s).length;
    const el = document.createElement('span');
    el.className = 'chip' + (stratOn[s] ? ' on' : '');
    el.textContent = `${s} (${n})`;
    el.onclick = () => { stratOn[s] = !stratOn[s]; buildChips(); rebuildPicker(); };
    host.appendChild(el);
  });
}

function visibleIndices() {
  const out = [];
  TRADES.forEach((t, i) => {
    if (stratOn[t.strategy || 'VP_TRAIL']) out.push(i);
  });
  return out;
}

const picker = document.getElementById('picker');

function rebuildPicker() {
  const idxs = visibleIndices();
  picker.innerHTML = '';
  idxs.forEach((i, k) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `[${k+1}/${idxs.length}] ${TRADES[i].label}`;
    picker.appendChild(opt);
  });
  document.getElementById('trade-count').textContent =
    `${idxs.length} of ${TRADES.length}`;
  if (idxs.length) render(idxs[0]);
}

buildChips();

function _parseTs(arr) { return arr.map(s => new Date(s)); }

function buildTraces(t) {
  const traces = [];
  const ts = _parseTs(t.candles.ts);

  // 70% VA bands drawn via shapes — see layout. Here just candles.
  traces.push({
    type: 'candlestick',
    x: ts,
    open: t.candles.o, high: t.candles.h, low: t.candles.l, close: t.candles.c,
    name: '15m',
    increasing: { line: { color: '#10b981' }, fillcolor: 'rgba(16,185,129,0.55)' },
    decreasing: { line: { color: '#ef4444' }, fillcolor: 'rgba(239,68,68,0.55)' },
    line: { width: 1 }, whiskerwidth: 0.4, xaxis: 'x', yaxis: 'y',
  });

  // Developing FULL profile VAH / VAL / POC step lines
  if (t.va_track_full && t.va_track_full.length) {
    const tts = _parseTs(t.va_track_full.map(r => r.ts));
    traces.push({
      type: 'scatter', mode: 'lines', x: tts,
      y: t.va_track_full.map(r => r.vah),
      line: { shape: 'hv', color: 'rgba(16,185,129,0.85)', width: 1.4 },
      name: 'full VAH', legendgroup: 'full', xaxis: 'x', yaxis: 'y',
    });
    traces.push({
      type: 'scatter', mode: 'lines', x: tts,
      y: t.va_track_full.map(r => r.val),
      line: { shape: 'hv', color: 'rgba(239,68,68,0.85)', width: 1.4 },
      fill: 'tonexty', fillcolor: 'rgba(245,158,11,0.07)',
      name: 'full VAL', legendgroup: 'full', xaxis: 'x', yaxis: 'y',
    });
    traces.push({
      type: 'scatter', mode: 'lines', x: tts,
      y: t.va_track_full.map(r => r.poc),
      line: { shape: 'hv', color: 'rgba(245,158,11,0.95)', width: 1.2, dash: 'dot' },
      name: 'full POC', legendgroup: 'full', xaxis: 'x', yaxis: 'y',
    });
  }

  // Developing SUB profile (purple)
  if (t.va_track_sub && t.va_track_sub.length) {
    const sts = _parseTs(t.va_track_sub.map(r => r.ts));
    traces.push({
      type: 'scatter', mode: 'lines', x: sts,
      y: t.va_track_sub.map(r => r.vah),
      line: { shape: 'hv', color: 'rgba(168,85,247,0.95)', width: 1.5 },
      name: 'sub VAH', legendgroup: 'sub', xaxis: 'x', yaxis: 'y',
    });
    traces.push({
      type: 'scatter', mode: 'lines', x: sts,
      y: t.va_track_sub.map(r => r.val),
      line: { shape: 'hv', color: 'rgba(236,72,153,0.95)', width: 1.5 },
      fill: 'tonexty', fillcolor: 'rgba(168,85,247,0.08)',
      name: 'sub VAL', legendgroup: 'sub', xaxis: 'x', yaxis: 'y',
    });
    traces.push({
      type: 'scatter', mode: 'lines', x: sts,
      y: t.va_track_sub.map(r => r.poc),
      line: { shape: 'hv', color: 'rgba(126,34,206,0.95)', width: 1.2, dash: 'dot' },
      name: 'sub POC', legendgroup: 'sub', xaxis: 'x', yaxis: 'y',
    });
  }

  // Trade markers (entry, exit, stop, target)
  const tr = t.trade;
  if (tr.entry_ts) {
    const dirSym = (tr.direction === 'LONG') ? 'triangle-up' : 'triangle-down';
    const dirColor = (tr.direction === 'LONG') ? '#16a34a' : '#dc2626';
    let entryText, exitText;
    if (t.has_options && t.trade.opt) {
      const o = t.trade.opt;
      const cp = o.kind === 'C' ? 'CE' : 'PE';
      entryText = `BUY ${o.strike.toLocaleString()} ${cp} @ ₹${o.premium_in.toFixed(0)}`;
      exitText  = `SELL @ ₹${o.premium_out.toFixed(0)}  (${o.pnl_rs >= 0 ? '+' : ''}₹${o.pnl_rs.toLocaleString()})`;
    } else {
      entryText = `${tr.direction} @ ${tr.entry_px.toLocaleString()}`;
      exitText  = `${tr.exit_reason} ${tr.exit_px.toLocaleString()}`;
    }
    traces.push({
      type: 'scatter', mode: 'markers+text',
      x: [new Date(tr.entry_ts), new Date(tr.exit_ts)],
      y: [tr.entry_px, tr.exit_px],
      marker: { symbol: [dirSym, 'square'], size: [16, 12],
                color: [dirColor, '#6b7280'],
                line: { color: 'black', width: 1 } },
      text: [entryText, exitText],
      textposition: ['bottom right', 'top right'],
      textfont: { size: 11, color: 'black' },
      name: 'trade', xaxis: 'x', yaxis: 'y',
      hovertemplate: '%{text}<br>%{x|%Y-%m-%d %H:%M}<extra></extra>',
    });
  }

  // Right panel: full profile + sub profile horizontal bars
  if (t.full_profile && t.full_profile.bins.length) {
    traces.push({
      type: 'bar', orientation: 'h',
      x: t.full_profile.vols, y: t.full_profile.bins,
      marker: { color: 'rgba(37,99,235,0.45)',
                line: { color: '#1e3a8a', width: 0.3 } },
      name: 'full profile', xaxis: 'x2', yaxis: 'y2',
      hovertemplate: 'price ≥ %{y:,.0f}<br>vol %{x:,.0f}<extra>full</extra>',
    });
  }
  if (t.sub_profile && t.sub_profile.bins.length) {
    traces.push({
      type: 'bar', orientation: 'h',
      x: t.sub_profile.vols, y: t.sub_profile.bins,
      marker: { color: 'rgba(168,85,247,0.55)',
                line: { color: '#581c87', width: 0.3 } },
      name: 'sub profile', xaxis: 'x2', yaxis: 'y2',
      hovertemplate: 'price ≥ %{y:,.0f}<br>vol %{x:,.0f}<extra>sub</extra>',
    });
  }

  return traces;
}

function buildLayout(t) {
  const shapes = [];
  const annotations = [];

  // Force x-range to span the full candle data — Plotly's autorange with
  // candlestick + rangebreaks sometimes clips early at the last trade
  // marker position rather than at the last candle.
  const x_first = t.candles && t.candles.ts.length ? t.candles.ts[0] : null;
  const x_last  = t.candles && t.candles.ts.length
                  ? t.candles.ts[t.candles.ts.length - 1] : null;

  // 70% VA shaded rect (full profile)
  if (t.final_va_full && t.final_va_full.vah && t.final_va_full.val) {
    shapes.push({
      type: 'rect', xref: 'paper', yref: 'y',
      x0: 0, x1: 1,
      y0: t.final_va_full.val, y1: t.final_va_full.vah,
      fillcolor: 'rgba(37,99,235,0.06)', line: { width: 0 }, layer: 'below',
    });
    // dashed VAH/POC/VAL reference lines (full)
    [['vah','#10b981'], ['poc','#f59e0b'], ['val','#ef4444']].forEach(([k,col]) => {
      if (t.final_va_full[k] != null) {
        shapes.push({
          type: 'line', xref: 'paper', yref: 'y',
          x0: 0, x1: 1, y0: t.final_va_full[k], y1: t.final_va_full[k],
          line: { color: col, dash: 'dash', width: 1 },
        });
      }
    });
  }
  // ATM strike line (options trades only) — solid magenta horizontal
  if (t.has_options && t.trade.opt && t.trade.opt.strike) {
    shapes.push({
      type: 'line', xref: 'paper', yref: 'y',
      x0: 0, x1: 1,
      y0: t.trade.opt.strike, y1: t.trade.opt.strike,
      line: { color: '#c026d3', dash: 'solid', width: 1.6 },
    });
    annotations.push({
      x: 1, y: t.trade.opt.strike, xref: 'paper', yref: 'y',
      text: `Strike ${t.trade.opt.strike.toLocaleString()} ${t.trade.opt.kind === 'C' ? 'CE' : 'PE'}`,
      showarrow: false, font: { color: '#a21caf', size: 11 },
      xanchor: 'right', yshift: 8, xshift: -8,
    });
  }

  // 70% VA rect (sub profile, if present)
  if (t.final_va_sub && t.final_va_sub.vah && t.final_va_sub.val) {
    shapes.push({
      type: 'rect', xref: 'paper', yref: 'y',
      x0: 0, x1: 1,
      y0: t.final_va_sub.val, y1: t.final_va_sub.vah,
      fillcolor: 'rgba(168,85,247,0.10)', line: { width: 0 }, layer: 'below',
    });
  }

  // Vertical line at regime start (if any)
  if (t.regime_start) {
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper',
      x0: t.regime_start, x1: t.regime_start, y0: 0, y1: 1,
      line: { color: '#a855f7', dash: 'dash', width: 1.5 },
    });
    annotations.push({
      x: t.regime_start, y: 1, xref: 'x', yref: 'paper',
      text: `regime shift · ${t.regime_start}`,
      showarrow: false, font: { color: '#7e22ce', size: 11 },
      yshift: 8, xanchor: 'left',
    });
  }

  // ── Mother-Baby overlay ──────────────────────────────────────────────
  const mb = t.mother_baby;
  if (mb && mb.mother_ts) {
    const mTo = t.trade.entry_ts || mb.mother_ts;   // coil range ends at entry
    // Mother high / low — the coil range, dashed, mother_ts → entry
    shapes.push({
      type: 'line', xref: 'x', yref: 'y',
      x0: mb.mother_ts, x1: mTo, y0: mb.mother_high, y1: mb.mother_high,
      line: { color: '#2563eb', dash: 'dash', width: 1.6 },
    });
    shapes.push({
      type: 'line', xref: 'x', yref: 'y',
      x0: mb.mother_ts, x1: mTo, y0: mb.mother_low, y1: mb.mother_low,
      line: { color: '#2563eb', dash: 'dash', width: 1.6 },
    });
    // Mother candle highlight — amber vertical band
    shapes.push({
      type: 'rect', xref: 'x', yref: 'paper',
      x0: mb.mother_ts, x1: mb.mother_ts, y0: 0, y1: 1,
      line: { color: '#f59e0b', width: 8 }, opacity: 0.55,
    });
    annotations.push({
      x: mb.mother_ts, y: mb.mother_high, xref: 'x', yref: 'y',
      text: '<b>MOTHER</b>', showarrow: true, arrowhead: 2, ax: 0, ay: -28,
      font: { color: '#b45309', size: 12 },
      arrowcolor: '#f59e0b',
    });
    // Baby coil zone — shaded rect baby_first → baby_last × mother range
    if (mb.baby_first && mb.baby_last) {
      shapes.push({
        type: 'rect', xref: 'x', yref: 'y',
        x0: mb.baby_first, x1: mb.baby_last,
        y0: mb.mother_low, y1: mb.mother_high,
        fillcolor: 'rgba(16,185,129,0.14)',
        line: { color: '#10b981', width: 1, dash: 'dot' }, layer: 'below',
      });
      annotations.push({
        x: mb.baby_last, y: mb.mother_low, xref: 'x', yref: 'y',
        text: `<b>BABY ×${mb.n_babies}</b>`, showarrow: true, arrowhead: 2,
        ax: 0, ay: 26, font: { color: '#047857', size: 11 },
        arrowcolor: '#10b981',
      });
    }
    // v2: baby's high/low ARE the breakout reference — solid green lines
    // from the baby out to the entry point.
    const mEnd = t.trade.entry_ts || mb.baby_last || mb.mother_ts;
    if (mb.baby_high) {
      shapes.push({
        type: 'line', xref: 'x', yref: 'y',
        x0: mb.baby_first || mb.mother_ts, x1: mEnd,
        y0: mb.baby_high, y1: mb.baby_high,
        line: { color: '#059669', width: 1.4 },
      });
      annotations.push({
        x: mEnd, y: mb.baby_high, xref: 'x', yref: 'y',
        text: 'baby high (break ↑)', showarrow: false,
        font: { color: '#059669', size: 10 }, xanchor: 'right', yshift: 7,
      });
    }
    if (mb.baby_low) {
      shapes.push({
        type: 'line', xref: 'x', yref: 'y',
        x0: mb.baby_first || mb.mother_ts, x1: mEnd,
        y0: mb.baby_low, y1: mb.baby_low,
        line: { color: '#059669', width: 1.4 },
      });
      annotations.push({
        x: mEnd, y: mb.baby_low, xref: 'x', yref: 'y',
        text: 'baby low (break ↓)', showarrow: false,
        font: { color: '#059669', size: 10 }, xanchor: 'right', yshift: -7,
      });
    }
  }

  // Vertical line at the contract expiry (= last candle in the window)
  if (t.candles && t.candles.ts.length) {
    const expiry_ts = t.candles.ts[t.candles.ts.length - 1];
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper',
      x0: expiry_ts, x1: expiry_ts, y0: 0, y1: 1,
      line: { color: '#dc2626', dash: 'dot', width: 1.5 },
    });
    annotations.push({
      x: expiry_ts, y: 1, xref: 'x', yref: 'paper',
      text: `expiry · ${expiry_ts.slice(0, 10)}`,
      showarrow: false, font: { color: '#991b1b', size: 11 },
      yshift: 8, xanchor: 'right',
    });
  }
  // Other regime shift dates inside the window (lighter)
  (t.regime_shifts || []).forEach(d => {
    if (d === t.regime_start) return;
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper',
      x0: d, x1: d, y0: 0, y1: 1,
      line: { color: 'rgba(168,85,247,0.4)', dash: 'dot', width: 1 },
    });
  });

  // Trade stop / target horizontal lines (zoom-helpful)
  if (t.trade && t.trade.entry_ts && t.trade.exit_ts) {
    [['stop','#dc2626'], ['target','#16a34a']].forEach(([k,col]) => {
      if (t.trade[k]) {
        shapes.push({
          type: 'line', xref: 'x', yref: 'y',
          x0: t.trade.entry_ts, x1: t.trade.exit_ts,
          y0: t.trade[k], y1: t.trade[k],
          line: { color: col, dash: 'dot', width: 1 },
        });
      }
    });
  }

  return {
    title: { text: t.label, x: 0.01, xanchor: 'left',
             font: { size: 13 } },
    grid: { rows: 1, columns: 2, pattern: 'independent' },
    xaxis:  { domain: [0, 0.78], showgrid: true, gridcolor: '#e5e7eb',
              rangeslider: { visible: false },
              autorange: false,
              range: (x_first && x_last) ? [x_first, x_last] : undefined,
              type: 'date',
              rangebreaks: [
                { bounds: ['sat', 'mon'] },
                { bounds: [15.5, 9.25], pattern: 'hour' },
              ] },
    yaxis:  { showgrid: true, gridcolor: '#e5e7eb', title: 'price' },
    xaxis2: { domain: [0.79, 1.0], showgrid: false, zeroline: false,
              title: 'volume' },
    yaxis2: { matches: 'y', showgrid: false, anchor: 'x2',
              showticklabels: false },
    shapes, annotations,
    showlegend: true,
    legend: { orientation: 'h', y: -0.12, x: 0.5, xanchor: 'center' },
    margin: { l: 60, r: 20, t: 50, b: 70 },
    hovermode: 'x',
    barmode: 'overlay',
    plot_bgcolor: 'white', paper_bgcolor: 'white',
  };
}

let currentIdx = 0;
function render(idx) {
  currentIdx = idx;
  const t = TRADES[idx];
  document.getElementById('picker').value = idx;
  let metaText;
  if (t.has_options && t.trade.opt) {
    const o = t.trade.opt;
    const cp = o.kind === 'C' ? 'CE' : 'PE';
    metaText =
      `${t.contract}  ·  ${o.strike.toLocaleString()} ${cp} exp ${o.expiry} (${o.dte_entry}d→${o.dte_exit}d) · ` +
      `entry ${t.trade.entry_ts.replace('T',' ')} @ futures ${t.trade.entry_px.toLocaleString()} (premium ₹${o.premium_in.toFixed(0)}) · ` +
      `exit ${t.trade.exit_ts.replace('T',' ')} @ futures ${t.trade.exit_px.toLocaleString()} (premium ₹${o.premium_out.toFixed(0)}) · ` +
      `budget/lot ₹${o.budget_rs.toLocaleString()}  ·  ` +
      `OPT P&L ₹${o.pnl_rs.toLocaleString()} (${o.return_pct.toFixed(0)}% on premium) · ` +
      `[fut for ref: ₹${t.trade.pnl_rs.toLocaleString()}]`;
  } else {
    metaText =
      `${t.contract} · entry ${t.trade.entry_ts ? t.trade.entry_ts.replace('T',' ') : '?'} · ` +
      `exit ${t.trade.exit_ts ? t.trade.exit_ts.replace('T',' ') : '?'} · ` +
      `stop ${t.trade.stop?.toLocaleString() ?? '?'} · ` +
      `target ${t.trade.target?.toLocaleString() ?? '?'} · ` +
      `pnl ${t.trade.pnl_pts?.toFixed(1)} pts (₹${t.trade.pnl_rs?.toLocaleString()})`;
  }
  document.getElementById('meta').textContent = metaText;
  Plotly.react('chart', buildTraces(t), buildLayout(t),
               { responsive: true, displayModeBar: true });
}
function step(d) {
  const idxs = visibleIndices();
  if (!idxs.length) return;
  let pos = idxs.indexOf(currentIdx);
  if (pos < 0) pos = 0;
  let n = pos + d;
  if (n < 0) n = idxs.length - 1;
  if (n >= idxs.length) n = 0;
  render(idxs[n]);
}
window.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') step(-1);
  if (e.key === 'ArrowRight') step(1);
});

if (TRADES.length) { rebuildPicker(); }
else document.getElementById('chart').textContent = 'No trades to show.';
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--filter', default=None,
                    help='subset filter: instrument name (BANKNIFTY/NIFTY/'
                         'SENSEX), period (IS/OOS), or substring of contract')
    ap.add_argument('--max', type=int, default=None,
                    help='cap trade count (for smaller HTML)')
    ap.add_argument('--prefix', default='vpt_final',
                    help='VP-Trail prefix (vpt_final = canonical realistic-'
                         'slippage run)')
    ap.add_argument('--strategies',
                    default='VP_TRAIL,ORB,OPT_ORB,VWAP_REV,CREDIT_SPREAD,MOTHER_BABY_15M',
                    help='comma-separated strategies to include')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    want = {s.strip().upper() for s in args.strategies.split(',') if s.strip()}

    # ── Load instrument caches once ───────────────────────────────────────
    caches: dict = {}
    dailies: dict = {}
    for inst in ('BANKNIFTY', 'NIFTY', 'SENSEX'):
        df = _load_combined(inst)
        if df.empty:
            continue
        caches[inst]  = df
        dailies[inst] = _build_daily_summary(df)

    if not caches:
        sys.exit('no instrument caches in data/cache_15m/')

    # ── Collect (strategy, trade-row) pairs across all sources ────────────
    rows: list[tuple[str, pd.Series]] = []

    if 'VP_TRAIL' in want:
        for inst in ('BANKNIFTY', 'NIFTY', 'SENSEX'):
            t = _load_trades(inst, prefix=args.prefix)
            for _, r in t.iterrows():
                rows.append(('VP_TRAIL', r))

    fb = _load_full_backtest_trades()
    if not fb.empty:
        for strat in ('ORB', 'OPT_ORB', 'VWAP_REV'):
            if strat not in want:
                continue
            sub = fb[fb['strategy'] == strat]
            for _, r in sub.iterrows():
                rows.append((strat, r))

    if 'CREDIT_SPREAD' in want:
        sp = _load_credit_spread_trades()
        for _, r in sp.iterrows():
            rows.append(('CREDIT_SPREAD', r))

    if 'MOTHER_BABY_15M' in want:
        mb = _load_mother_baby_trades('15m')
        for _, r in mb.iterrows():
            rows.append(('MOTHER_BABY_15M', r))

    # ── Filter ────────────────────────────────────────────────────────────
    if args.filter:
        f = args.filter.upper()
        rows = [(s, r) for (s, r) in rows
                if (f in str(r.get('instrument_name', '')).upper()
                    or f == str(r.get('period', '')).upper()
                    or f in str(r.get('contract', '')).upper()
                    or f == s)]

    if args.max:
        rows = rows[:args.max]

    print(f'Building dataset for {len(rows)} trades '
          f'across {len({s for s, _ in rows})} strategies …')

    datasets: list = []
    for i, (strat, row) in enumerate(rows):
        inst = row.get('instrument_name')
        if inst not in caches:
            continue
        try:
            if strat == 'VP_TRAIL':
                ds = _build_trade_dataset(row, caches[inst], dailies[inst])
            else:
                ds = _build_simple_dataset(row, caches[inst], strat)
            if ds is not None:
                datasets.append(ds)
        except Exception as e:
            print(f'  ✗ trade {i} {strat} {inst} {row.get("date")}: {e}')

    if not datasets:
        sys.exit('no datasets built')

    out_path = pathlib.Path(args.out) if args.out \
        else ROOT / 'research' / 'trade_explorer.html'

    html = (_HTML_TEMPLATE
            .replace('${TRADE_COUNT}', str(len(datasets)))
            .replace('${TRADES_JSON}', json.dumps(datasets, default=str)))
    out_path.write_text(html, encoding='utf-8')

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f'✓ wrote {out_path}  ({size_mb:.1f} MB, {len(datasets)} trades)')


if __name__ == '__main__':
    main()
