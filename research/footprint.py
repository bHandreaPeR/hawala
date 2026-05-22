"""research/footprint.py — Bid×Ask footprint chart builder + HTML inspector.

Phase 2 of the footprint pipeline. Reads the daily tick CSV produced by
`alerts/tick_recorder.py`, bins prints into (candle_ts × price_level) cells,
and renders the canonical two-column footprint:

  • Each cell drawn as TWO rectangles per price level:
        left  = sell volume / count, red-shaded by magnitude
        right = buy  volume / count, green-shaded by magnitude
  • Bold yellow ring  → imbalance (one side ≥ IMB_MULT × other, ≥ IMB_MIN_TICKS)
  • Yellow background → POC row inside the candle
  • Faint green band  → value area (VAH..VAL, 70% volume)
  • Candle body + wick drawn in the gutter between the two columns
  • Right-side pane  → cumulative volume profile across the whole session
  • Bottom pane      → per-candle delta histogram (colour-coded)
  • Multi-aggregation: cell-size dropdown (fine / default / coarse)
  • Stacked-imbalance brackets on price axis when ≥3 same-side adjacent

Usage:
    python research/footprint.py --inst NIFTY     --date 2026-05-21 --tf 5min
    python research/footprint.py --inst BANKNIFTY --date 2026-05-21 --tf 15min
"""
from __future__ import annotations

import argparse
import pathlib
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / 'v3' / 'cache'
OUT_DIR   = ROOT / 'research'

DEFAULT_CELL = {'NIFTY': 5.0, 'BANKNIFTY': 10.0}
MULTI_CELLS  = {                                  # for the dropdown
    'NIFTY':     [2.0, 5.0, 10.0],
    'BANKNIFTY': [5.0, 10.0, 20.0],
}
IMB_MULT       = 3.0
IMB_MIN_TICKS  = 4
STACK_MIN      = 3        # ≥3 same-side adjacent imbalances → "stacked"
VA_PCT         = 0.70


# ─── Loaders ─────────────────────────────────────────────────────────────────
def load_ticks(inst: str, day: str) -> pd.DataFrame:
    stem = CACHE_DIR / f'ticks_{inst}_{day.replace("-","")}'
    p_csv, p_pq = stem.with_suffix('.csv'), stem.with_suffix('.parquet')
    if p_csv.exists():
        df = pd.read_csv(p_csv)
    elif p_pq.exists():
        df = pd.read_parquet(p_pq)
    else:
        raise FileNotFoundError(f'tick file missing: {p_csv}')
    df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms', utc=True).dt.tz_convert(
        'Asia/Kolkata').dt.tz_localize(None)
    return df.sort_values('ts').reset_index(drop=True)


def load_v3_trades(inst: str, day: str) -> pd.DataFrame:
    p = ROOT / 'trade_logs' / f'v3_{inst.lower()}_trades.csv'
    if not p.exists():
        return pd.DataFrame()
    try:
        t = pd.read_csv(p)
        ts_col = next((k for k in ('entry_ts','entry_time','timestamp','ts')
                       if k in t.columns), None)
        if ts_col is None:
            return pd.DataFrame()
        t['entry_dt'] = pd.to_datetime(t[ts_col])
        return t[t['entry_dt'].dt.date == pd.to_datetime(day).date()] \
                .reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


# ─── Footprint builder ───────────────────────────────────────────────────────
def build_for_cell(df: pd.DataFrame, tf: str, cell_size: float) -> dict:
    """Build per-(candle,price-cell) and per-candle aggregates for a given
    cell_size. Returns dict with two DataFrames + a 'profile' (vol per cell)."""
    if df.empty:
        return {'candles': pd.DataFrame(), 'cells': pd.DataFrame(),
                'profile': pd.DataFrame(), 'stacks': []}

    df = df.copy()
    df['bucket'] = df['ts'].dt.floor(tf)
    df['cell']   = (df['price'] / cell_size).round() * cell_size

    sign = df['side'].map({'BUY': 1, 'SELL': -1}).fillna(0).astype(int)
    df['buy_ticks']  = (sign ==  1).astype(int)
    df['sell_ticks'] = (sign == -1).astype(int)
    df['buy_qty']    = np.where(sign ==  1, df['qty'], 0.0)
    df['sell_qty']   = np.where(sign == -1, df['qty'], 0.0)

    cells = df.groupby(['bucket', 'cell'], as_index=False).agg(
        buy_ticks =('buy_ticks',  'sum'),
        sell_ticks=('sell_ticks', 'sum'),
        buy_qty   =('buy_qty',    'sum'),
        sell_qty  =('sell_qty',   'sum'),
    )
    cells['total_ticks'] = cells['buy_ticks'] + cells['sell_ticks']
    cells['total_qty']   = cells['buy_qty']   + cells['sell_qty']
    cells['delta_ticks'] = cells['buy_ticks'] - cells['sell_ticks']
    cells['delta_qty']   = cells['buy_qty']   - cells['sell_qty']

    def _imb(row):
        b, s, t = row['buy_ticks'], row['sell_ticks'], row['total_ticks']
        if t < IMB_MIN_TICKS:
            return None
        if b >= IMB_MULT * max(s, 1): return 'BUY'
        if s >= IMB_MULT * max(b, 1): return 'SELL'
        return None
    cells['imbalance'] = cells.apply(_imb, axis=1)

    candles = df.groupby('bucket', as_index=False).agg(
        open =('price', 'first'), high=('price', 'max'),
        low  =('price', 'min'),   close=('price', 'last'),
        n_ticks=('price','count'),
    )
    cd = cells.groupby('bucket', as_index=False).agg(
        delta_ticks=('delta_ticks','sum'),
        delta_qty  =('delta_qty',  'sum'),
        total_qty  =('total_qty',  'sum'),
    )
    candles = candles.merge(cd, on='bucket')
    candles['cvd_qty']   = candles['delta_qty'].cumsum()
    candles['cvd_ticks'] = candles['delta_ticks'].cumsum()

    # POC + value area per candle
    poc, vah, val = [], [], []
    for bk, sub in cells.groupby('bucket'):
        sub = sub.sort_values('cell')
        metric = sub['total_qty'] if sub['total_qty'].sum() > 0 \
            else sub['total_ticks']
        idx_max = metric.idxmax()
        poc_v = float(sub.loc[idx_max, 'cell'])
        order  = metric.sort_values(ascending=False).index.tolist()
        chosen = {idx_max}; acc = float(metric.loc[idx_max])
        target = VA_PCT * float(metric.sum())
        for idx in order[1:]:
            if acc >= target: break
            chosen.add(idx); acc += float(metric.loc[idx])
        lv = sub.loc[list(chosen), 'cell']
        poc.append(poc_v); val.append(float(lv.min())); vah.append(float(lv.max()))
    candles['poc'] = poc; candles['val'] = val; candles['vah'] = vah

    # Absorption flag
    candles['body'] = (candles['close'] - candles['open']).abs()
    if len(candles) >= 4:
        dq75 = candles['delta_ticks'].abs().quantile(0.75)
        bd25 = candles['body'].quantile(0.25)
        candles['absorbed'] = ((candles['delta_ticks'].abs() >= dq75)
                               & (candles['body'] <= bd25))
    else:
        candles['absorbed'] = False

    # Session-wide volume profile (per price-cell, summed across day)
    profile = cells.groupby('cell', as_index=False).agg(
        buy_qty=('buy_qty','sum'), sell_qty=('sell_qty','sum'),
        buy_ticks=('buy_ticks','sum'), sell_ticks=('sell_ticks','sum'),
    )
    profile['total_qty']   = profile['buy_qty']   + profile['sell_qty']
    profile['total_ticks'] = profile['buy_ticks'] + profile['sell_ticks']

    # Stacked imbalance scan — ≥STACK_MIN same-side adjacent (within ANY candle)
    stacks = []
    for bk, sub in cells.groupby('bucket'):
        sub = sub.sort_values('cell').reset_index(drop=True)
        run_side, run_start = None, None
        for i, r in sub.iterrows():
            if r['imbalance'] == run_side and run_side in ('BUY','SELL'):
                continue
            if run_side and run_start is not None:
                run_len = i - run_start
                if run_len >= STACK_MIN:
                    stacks.append({
                        'bucket': bk, 'side': run_side,
                        'p_lo': float(sub.loc[run_start, 'cell']),
                        'p_hi': float(sub.loc[i - 1, 'cell']),
                        'n': int(run_len),
                    })
            run_side  = r['imbalance']
            run_start = i if r['imbalance'] in ('BUY','SELL') else None
        if run_side and run_start is not None:
            run_len = len(sub) - run_start
            if run_len >= STACK_MIN:
                stacks.append({
                    'bucket': bk, 'side': run_side,
                    'p_lo': float(sub.loc[run_start, 'cell']),
                    'p_hi': float(sub.loc[len(sub)-1, 'cell']),
                    'n': int(run_len),
                })

    return {'candles': candles.reset_index(drop=True),
            'cells':   cells.reset_index(drop=True),
            'profile': profile.reset_index(drop=True),
            'stacks':  stacks}


# ─── Two-column footprint renderer ───────────────────────────────────────────
def _color(side: str, intensity: float) -> str:
    """Shade red (sell) or green (buy) by [0..1] intensity. ≥0.85 = saturated."""
    t = max(0.0, min(1.0, intensity))
    if side == 'BUY':
        # green: rgba(38, 166, 154, alpha) - vivid teal-green
        a = 0.10 + 0.75 * t
        return f'rgba(38,166,154,{a:.2f})'
    # red: rgba(239, 83, 80, alpha)
    a = 0.10 + 0.75 * t
    return f'rgba(239,83,80,{a:.2f})'


def _build_candle_traces(fp: dict, tf: str, cell_size: float,
                         visible: bool) -> tuple[list, list, list]:
    """Return (shape_dicts, annotation_dicts, scatter_traces) for one cell-
    size aggregation. Visibility toggled via the layout dropdown."""
    candles, cells, stacks = fp['candles'], fp['cells'], fp['stacks']
    if candles.empty:
        return [], [], []

    tf_td  = pd.Timedelta(tf)
    half_w = tf_td * 0.40      # cell column width (each side)
    body_w = tf_td * 0.09      # centre gutter for the candle wick + body

    shapes, annos, traces = [], [], []

    # Value-area light-green band across all candles, drawn per candle for clarity
    for _, c in candles.iterrows():
        x_cen = c['bucket']
        x0 = x_cen - half_w; x1 = x_cen + half_w
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x0, x1=x1,
            y0=c['val'] - cell_size / 2, y1=c['vah'] + cell_size / 2,
            fillcolor='rgba(46,125,50,0.06)',
            line=dict(width=0), layer='below',
            visible=visible,
        ))

    # Normalise per-candle so a busy bucket doesn't wash out neighbouring quiet ones
    cell_max_per_bucket = cells.groupby('bucket')[['buy_qty','sell_qty',
                                                   'buy_ticks','sell_ticks']].max()

    use_qty = cells['total_qty'].sum() > 0
    metric_b = 'buy_qty'  if use_qty else 'buy_ticks'
    metric_s = 'sell_qty' if use_qty else 'sell_ticks'

    for _, r in cells.iterrows():
        bk = r['bucket']; cell = r['cell']
        x_cen = bk
        y0 = cell - cell_size / 2; y1 = cell + cell_size / 2
        mb = float(cell_max_per_bucket.loc[bk, metric_b]) or 1.0
        ms = float(cell_max_per_bucket.loc[bk, metric_s]) or 1.0

        sell_v = float(r[metric_s]); buy_v = float(r[metric_b])
        s_int = sell_v / ms; b_int = buy_v / mb

        # LEFT (sell)
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x_cen - half_w,         x1=x_cen - body_w,
            y0=y0, y1=y1,
            fillcolor=_color('SELL', s_int),
            line=dict(width=0.3, color='rgba(0,0,0,0.18)'),
            layer='above',
            visible=visible,
        ))
        # RIGHT (buy)
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x_cen + body_w, x1=x_cen + half_w,
            y0=y0, y1=y1,
            fillcolor=_color('BUY', b_int),
            line=dict(width=0.3, color='rgba(0,0,0,0.18)'),
            layer='above',
            visible=visible,
        ))
        # Imbalance highlight — bold golden ring on the dominant side
        if r['imbalance'] == 'BUY':
            shapes.append(dict(
                type='rect', xref='x', yref='y',
                x0=x_cen + body_w, x1=x_cen + half_w,
                y0=y0, y1=y1,
                fillcolor='rgba(255,193,7,0.0)',
                line=dict(width=2, color='#ffb300'),
                visible=visible,
            ))
        elif r['imbalance'] == 'SELL':
            shapes.append(dict(
                type='rect', xref='x', yref='y',
                x0=x_cen - half_w, x1=x_cen - body_w,
                y0=y0, y1=y1,
                fillcolor='rgba(255,193,7,0.0)',
                line=dict(width=2, color='#ffb300'),
                visible=visible,
            ))

        # Text — show qty (or ticks when qty zero). Bold the imbalanced side.
        def _txt(v):
            if v == 0: return '0'
            return str(int(v)) if v == int(v) else f'{v:.0f}'

        annos.append(dict(
            x=x_cen - (half_w + body_w) / 2, y=cell,
            text=f"<b>{_txt(sell_v)}</b>" if r['imbalance']=='SELL' else _txt(sell_v),
            xref='x', yref='y',
            showarrow=False,
            font=dict(size=9, color='#b71c1c' if sell_v>0 else '#999'),
            visible=visible,
        ))
        annos.append(dict(
            x=x_cen + (half_w + body_w) / 2, y=cell,
            text=f"<b>{_txt(buy_v)}</b>" if r['imbalance']=='BUY' else _txt(buy_v),
            xref='x', yref='y',
            showarrow=False,
            font=dict(size=9, color='#0d6e63' if buy_v>0 else '#999'),
            visible=visible,
        ))

    # Candle wick + body in the centre gutter
    for _, c in candles.iterrows():
        x_cen = c['bucket']
        body_color = '#26a69a' if c['close'] >= c['open'] else '#ef5350'
        # wick
        shapes.append(dict(
            type='line', xref='x', yref='y',
            x0=x_cen, x1=x_cen, y0=c['low'], y1=c['high'],
            line=dict(color=body_color, width=1.2),
            visible=visible,
        ))
        # body (thin strip in the centre)
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x_cen - body_w, x1=x_cen + body_w,
            y0=min(c['open'], c['close']),
            y1=max(c['open'], c['close']),
            fillcolor=body_color,
            line=dict(width=0),
            visible=visible,
        ))
        # POC ring on the POC row
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x_cen - half_w, x1=x_cen + half_w,
            y0=c['poc'] - cell_size / 2, y1=c['poc'] + cell_size / 2,
            fillcolor='rgba(255,235,59,0.0)',
            line=dict(width=1.5, color='#fdd835'),
            visible=visible,
        ))

    # Stacked-imbalance bracket on the y-axis (paper xref)
    for s in stacks:
        col = '#0d6e63' if s['side'] == 'BUY' else '#b71c1c'
        x_cen = s['bucket']
        shapes.append(dict(
            type='rect', xref='x', yref='y',
            x0=x_cen + half_w*0.95, x1=x_cen + half_w*1.05,
            y0=s['p_lo'] - cell_size/2, y1=s['p_hi'] + cell_size/2,
            fillcolor=col, line=dict(width=0),
            visible=visible,
        ))

    return shapes, annos, traces


def render_html(df: pd.DataFrame, inst: str, day: str, tf: str,
                cell_sizes: list[float], trades: Optional[pd.DataFrame],
                out: pathlib.Path) -> None:
    if df.empty:
        out.write_text('<h3>No data for the chosen window.</h3>')
        return

    # Build aggregations for every cell-size choice in advance
    aggs = {cs: build_for_cell(df, tf, cs) for cs in cell_sizes}

    fig = make_subplots(
        rows=2, cols=2,
        shared_xaxes=False,
        column_widths=[0.83, 0.17],
        row_heights=[0.78, 0.22],
        horizontal_spacing=0.01, vertical_spacing=0.05,
        specs=[
            [{'type':'xy'},                {'type':'xy'}],
            [{'type':'xy', 'colspan':2},   None],
        ],
        subplot_titles=('', 'session vol profile', 'per-candle delta (qty)'),
    )

    # ── ANCHOR TRACE ─────────────────────────────────────────────────────────
    # Plotly needs at least one trace per subplot to infer axis type+range. We
    # only draw shapes/annotations on the main pane — without an anchor, the
    # x-axis falls back to numeric 0..N and our datetime-keyed shapes collapse
    # to the left edge. Drop an invisible scatter spanning the price/time
    # extremes so the axis is typed correctly.
    cs_default = cell_sizes[len(cell_sizes)//2]
    cd0 = aggs[cs_default]['candles']
    if not cd0.empty:
        tf_td = pd.Timedelta(tf)
        anchor_x = [cd0['bucket'].min() - tf_td*0.6,
                    cd0['bucket'].max() + tf_td*0.6]
        anchor_y = [cd0['low'].min()  - 1.5 * cell_sizes[0],
                    cd0['high'].max() + 1.5 * cell_sizes[0]]
        fig.add_trace(go.Scatter(
            x=anchor_x, y=anchor_y, mode='markers',
            marker=dict(size=0.1, color='rgba(0,0,0,0)'),
            hoverinfo='skip', showlegend=False, name='_anchor',
        ), row=1, col=1)

    # Build cells/candles for each cell-size — visibility toggled by dropdown
    all_shapes: list[dict] = []
    all_annos:  list[dict] = []
    cs_shape_ranges: dict[float, tuple[int,int]] = {}
    cs_anno_ranges:  dict[float, tuple[int,int]] = {}

    for cs in cell_sizes:
        s0, a0 = len(all_shapes), len(all_annos)
        sh, an, _ = _build_candle_traces(aggs[cs], tf, cs, visible=(cs == cs_default))
        all_shapes.extend(sh); all_annos.extend(an)
        cs_shape_ranges[cs] = (s0, len(all_shapes))
        cs_anno_ranges[cs]  = (a0, len(all_annos))

    # Session profile (right pane) — based on the DEFAULT cell-size aggregation
    prof = aggs[cs_default]['profile'].sort_values('cell')
    fig.add_trace(go.Bar(
        x=-prof['sell_qty'] if prof['sell_qty'].sum() > 0 else -prof['sell_ticks'],
        y=prof['cell'], orientation='h',
        marker=dict(color='rgba(239,83,80,0.85)'),
        name='sell vol', showlegend=False,
        hovertemplate='price=%{y}<br>sell=%{x:.0f}<extra></extra>',
    ), row=1, col=2)
    fig.add_trace(go.Bar(
        x=prof['buy_qty'] if prof['buy_qty'].sum() > 0 else prof['buy_ticks'],
        y=prof['cell'], orientation='h',
        marker=dict(color='rgba(38,166,154,0.85)'),
        name='buy vol', showlegend=False,
        hovertemplate='price=%{y}<br>buy=%{x:.0f}<extra></extra>',
    ), row=1, col=2)
    fig.update_layout(barmode='overlay')

    # Per-candle delta histogram (bottom)
    candles_def = aggs[cs_default]['candles']
    colors = ['#26a69a' if d >= 0 else '#ef5350' for d in candles_def['delta_qty']]
    fig.add_trace(go.Bar(
        x=candles_def['bucket'], y=candles_def['delta_qty'],
        marker=dict(color=colors), name='delta', showlegend=False,
        hovertemplate='%{x}<br>delta_qty=%{y}<extra></extra>',
    ), row=2, col=1)

    # V3 trade overlay
    if trades is not None and not trades.empty:
        for _, r in trades.iterrows():
            try:
                ts = r['entry_dt']
                px = float(r.get('entry') or r.get('entry_price') or 0)
                side = str(r.get('side') or r.get('direction') or '')
                col  = '#2e7d32' if 'LONG' in side.upper() else '#c62828'
                fig.add_trace(go.Scatter(
                    x=[ts], y=[px], mode='markers',
                    marker=dict(symbol='star', size=15, color=col,
                                line=dict(width=1.5, color='black')),
                    name=side, showlegend=False,
                    hovertemplate=f'{side} @ {px}<extra></extra>',
                ), row=1, col=1)
            except Exception:
                continue

    # Dropdown to switch cell size — must preserve subplot title annotations
    base_annos = list(fig.layout.annotations)            # subplot titles
    n_shapes = len(all_shapes); n_annos = len(all_annos)
    buttons = []
    for cs in cell_sizes:
        sh_vis = [False] * n_shapes
        an_vis = [False] * n_annos
        s0, s1 = cs_shape_ranges[cs]
        a0, a1 = cs_anno_ranges[cs]
        for i in range(s0, s1): sh_vis[i] = True
        for i in range(a0, a1): an_vis[i] = True

        new_shapes = [{**all_shapes[i], 'visible': sh_vis[i]} for i in range(n_shapes)]
        new_annos  = base_annos + \
            [{**all_annos[i],  'visible': an_vis[i]} for i in range(n_annos)]
        buttons.append(dict(
            label=f'cell={cs:g}',
            method='relayout',
            args=[{'shapes': new_shapes, 'annotations': new_annos}],
        ))

    # Match x-range between main and delta panes so bars line up under candles
    if not cd0.empty:
        x_range = [cd0['bucket'].min() - tf_td*0.6,
                   cd0['bucket'].max() + tf_td*0.6]
    else:
        x_range = None

    fig.update_layout(
        shapes=all_shapes,
        annotations=base_annos + all_annos,
        height=950, template='plotly_white',
        margin=dict(l=50, r=20, t=70, b=30),
        title=dict(text=f'{inst} footprint · {day} · tf={tf}',
                   x=0.01, xanchor='left', y=0.985),
        xaxis=dict(title='', type='date',
                   **({'range': x_range} if x_range else {})),
        yaxis=dict(title='price'),
        xaxis2=dict(title='vol', showticklabels=False),
        yaxis2=dict(matches='y', showticklabels=False),
        xaxis3=dict(title='', type='date',
                    **({'range': x_range} if x_range else {})),
        yaxis3=dict(title='Δ qty'),
        updatemenus=[dict(
            buttons=buttons, direction='right',
            x=0.30, y=1.05, xanchor='left', yanchor='top',
            showactive=True, type='buttons',
            bgcolor='#eceff1',
        )],
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs='cdn', full_html=True)


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--inst', required=True, choices=['NIFTY', 'BANKNIFTY'])
    p.add_argument('--date', required=True, help='YYYY-MM-DD')
    p.add_argument('--tf',   default='5min')
    p.add_argument('--cells', default=None,
                   help='comma-sep cell-sizes for dropdown; '
                        'default per-inst (NIFTY:2,5,10  BN:5,10,20)')
    p.add_argument('--out',  default=None)
    args = p.parse_args()

    cells = ([float(x) for x in args.cells.split(',')] if args.cells
             else MULTI_CELLS[args.inst])

    df = load_ticks(args.inst, args.date)
    print(f'  loaded {len(df)} ticks  '
          f'{df["ts"].min() if not df.empty else "—"} → '
          f'{df["ts"].max() if not df.empty else "—"}')

    trades = load_v3_trades(args.inst, args.date)
    if not trades.empty:
        print(f'  v3 trades overlay: {len(trades)}')

    out = pathlib.Path(args.out) if args.out else \
        OUT_DIR / f'footprint_{args.inst}_{args.date}.html'
    render_html(df, args.inst, args.date, args.tf, cells, trades, out)
    print(f'  cell-sizes: {cells}')
    print(f'  → {out}')


if __name__ == '__main__':
    main()
