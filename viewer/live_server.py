"""viewer/live_server.py — Live footprint viewer backend.

FastAPI app that:
  • Serves the single-page HTML at  /
  • Provides REST snapshot at        /snapshot?inst=&date=&tf=&cell=
  • Streams tick deltas over WS at   /ws?inst=
  • Static JS / CSS from viewer/static/

Decoupled from the tick recorder. The recorder writes
`v3/cache/ticks_<INST>_<YYYYMMDD>.csv` continuously (cron 09:12). This
server just tails those files. It can be started before, after, or
without the recorder — and survives recorder restarts.

Run (dev):     python -m viewer.live_server
Run (prod):    /opt/anaconda3/bin/python3 -m viewer.live_server --host 0.0.0.0 --port 8765
Cron:          13 9 * * 1-5 ... nohup ... viewer.live_server > viewer.log 2>&1 &
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import pathlib
import pickle
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT      = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / 'v3' / 'cache'
STATIC    = pathlib.Path(__file__).resolve().parent / 'static'

INSTRUMENTS = ['NIFTY', 'BANKNIFTY']
DEFAULT_CELL = {'NIFTY': 5.0, 'BANKNIFTY': 10.0}
TF_CHOICES   = ['1min', '3min', '5min', '15min']
CELL_CHOICES = {'NIFTY': [2, 5, 10], 'BANKNIFTY': [5, 10, 20]}

IMB_MULT, IMB_MIN_TICKS, VA_PCT = 3.0, 4, 0.70

# Stall threshold for the WS streamer. If we see no new ticks for this long
# during market hours, surface a {'type':'stall'} message so the UI can show
# "feed stale" — the recorder's own watchdog will already be reconnecting.
WS_STALL_THRESHOLD_SEC = 90.0

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('viewer')

app = FastAPI(title='Hawala live footprint')
app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')


def _market_hours_now() -> bool:
    """True if `now` is on a weekday between 09:15 and 15:35 IST (server
    is assumed to run in IST, matching the recorder cron). Used to gate
    WS stall warnings so we don't fire them overnight."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 35)


# ─── tick file IO ────────────────────────────────────────────────────────────
def _tick_path(inst: str, day: str) -> pathlib.Path:
    # v1 (alerts.tick_recorder) now carries the qty=0 filter + reversal
    # handling backported from the v2 experiment. v2 recorder retired.
    return CACHE_DIR / f'ticks_{inst}_{day.replace("-","")}.csv'


_CSV_COLS = ['ts_ms','inst','price','qty','side','rule','bid','ask',
             'bid_qty','ask_qty','spread','cum_volume']


def _read_ticks(inst: str, day: str, since_byte: int
                ) -> tuple[pd.DataFrame, int]:
    """Read NEW ticks from byte offset to last complete newline.

    Important: NEVER replays the whole file. Caller is responsible for
    initialising since_byte to a sane EOF on first call — if since_byte is
    < 0 (sentinel for "uninitialised"), we just return (empty, current EOF)
    so the next poll starts streaming from there.

    Returns (df, new_byte_offset). new_byte_offset always points to the
    byte AFTER the last complete row consumed — never mid-row — so the
    next call resumes cleanly without dropping or duplicating rows.
    """
    p = _tick_path(inst, day)
    if not p.exists():
        # Preserve the uninitialised sentinel so when the file finally
        # appears, we jump to its EOF instead of replaying everything.
        return pd.DataFrame(), since_byte if since_byte < 0 else 0
    size = p.stat().st_size
    # Sentinel: caller hasn't initialised. Don't replay history; just
    # advance to current EOF and the next poll will stream genuinely new
    # rows.
    if since_byte < 0:
        return pd.DataFrame(), size
    if since_byte >= size:
        return pd.DataFrame(), since_byte
    with p.open('rb') as f:
        f.seek(since_byte)
        chunk = f.read()
    # Trim to last complete newline so we never parse a half-written row.
    nl = chunk.rfind(b'\n')
    if nl < 0:
        return pd.DataFrame(), since_byte           # no complete row yet
    consumed_to = since_byte + nl + 1
    text = chunk[:nl + 1].decode('utf-8', errors='ignore')
    # If since_byte was mid-row (e.g. WS started mid-write), the first
    # line is partial garbage — drop it. We can detect that by checking
    # whether since_byte was preceded by a newline.
    drop_first = False
    if since_byte > 0:
        with p.open('rb') as f:
            f.seek(since_byte - 1)
            prev = f.read(1)
        if prev != b'\n':
            drop_first = True
    lines = text.splitlines()
    if drop_first and lines:
        lines = lines[1:]
    if not lines:
        return pd.DataFrame(), consumed_to
    from io import StringIO
    try:
        tail = pd.read_csv(StringIO('\n'.join(lines)),
                           names=_CSV_COLS, header=None)
    except Exception:
        return pd.DataFrame(), consumed_to
    return tail, consumed_to


# ─── footprint aggregation (rebuilds snapshot from full CSV) ─────────────────
def _build_snapshot(inst: str, day: str, tf: str, cell_size: float) -> dict:
    p = _tick_path(inst, day)
    if not p.exists():
        return {'inst': inst, 'day': day, 'tf': tf, 'cell': cell_size,
                'candles': [], 'cells': [], 'profile': []}
    df = pd.read_csv(p)
    if df.empty:
        return {'inst': inst, 'day': day, 'tf': tf, 'cell': cell_size,
                'candles': [], 'cells': [], 'profile': []}
    # Bucket in raw UTC ms — keeps server/client math identical and lets the
    # browser handle Asia/Kolkata display via its native locale. Earlier we
    # tz_localize(None) then cast to int64 → shifted by +5:30h. Don't.
    tf_ms = int(pd.Timedelta(tf).total_seconds() * 1000)
    df['bucket'] = (df['ts_ms'].astype('int64') // tf_ms) * tf_ms
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

    def _imb(row):
        b, s, t = row['buy_ticks'], row['sell_ticks'], row['total_ticks']
        if t < IMB_MIN_TICKS: return None
        if b >= IMB_MULT * max(s, 1): return 'BUY'
        if s >= IMB_MULT * max(b, 1): return 'SELL'
        return None
    cells['imbalance'] = cells.apply(_imb, axis=1)

    candles = df.groupby('bucket', as_index=False).agg(
        open=('price','first'), high=('price','max'),
        low=('price','min'),    close=('price','last'),
    )
    cd = cells.groupby('bucket', as_index=False).agg(
        delta_qty=('buy_qty',  lambda x: x.sum() - cells.loc[x.index,'sell_qty'].sum()),
    )
    # safer recompute
    cd_buy  = cells.groupby('bucket')['buy_qty'].sum()
    cd_sell = cells.groupby('bucket')['sell_qty'].sum()
    candles['delta_qty'] = candles['bucket'].map(cd_buy - cd_sell).fillna(0)
    candles['cvd_qty']   = candles['delta_qty'].cumsum()

    # POC + value area
    poc, vah, val = [], [], []
    for bk, sub in cells.groupby('bucket'):
        sub = sub.sort_values('cell')
        metric = sub['total_qty'] if sub['total_qty'].sum() > 0 else sub['total_ticks']
        idx = metric.idxmax()
        poc_v = float(sub.loc[idx, 'cell'])
        order = metric.sort_values(ascending=False).index.tolist()
        chosen = {idx}; acc = float(metric.loc[idx])
        target = VA_PCT * float(metric.sum())
        for i in order[1:]:
            if acc >= target: break
            chosen.add(i); acc += float(metric.loc[i])
        lv = sub.loc[list(chosen), 'cell']
        poc.append(poc_v); val.append(float(lv.min())); vah.append(float(lv.max()))
    candles['poc'] = poc; candles['val'] = val; candles['vah'] = vah

    profile = cells.groupby('cell', as_index=False).agg(
        buy_qty=('buy_qty','sum'), sell_qty=('sell_qty','sum'),
        buy_ticks=('buy_ticks','sum'), sell_ticks=('sell_ticks','sum'),
    )

    def _df_records(d: pd.DataFrame) -> list:
        d = d.copy()
        if 'bucket' in d.columns:
            d['bucket'] = d['bucket'].astype('int64')          # already UTC ms
        for c in ('cell','open','high','low','close','poc','vah','val',
                  'delta_qty','cvd_qty','buy_qty','sell_qty','total_qty'):
            if c in d.columns:
                d[c] = d[c].astype(float)
        for c in ('buy_ticks','sell_ticks','total_ticks'):
            if c in d.columns:
                d[c] = d[c].astype(int)
        return d.where(pd.notnull(d), None).to_dict(orient='records')

    return {
        'inst': inst, 'day': day, 'tf': tf, 'cell': cell_size,
        'candles': _df_records(candles),
        'cells':   _df_records(cells),
        'profile': _df_records(profile),
        'n_ticks': int(len(df)),
        'last_ts': int(df['ts_ms'].max()) if not df.empty else 0,
    }


# ─── REST endpoints ──────────────────────────────────────────────────────────
@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    # Cache-bust app.js + style.css by file mtime so a browser never serves
    # a stale JS against a fresh HTML (which caused the 'stuck on loading'
    # bug when an old cached app.js referenced DOM the new HTML lacked, or
    # vice-versa). Appends ?v=<mtime> to each asset reference.
    for asset in ('app.js', 'style.css'):
        p = STATIC / asset
        if p.exists():
            ver = int(p.stat().st_mtime)
            html = html.replace(f'/static/{asset}', f'/static/{asset}?v={ver}')
    return HTMLResponse(html)


@app.get('/config')
async def config():
    return {
        'instruments': INSTRUMENTS,
        'tfs': TF_CHOICES,
        'cells': CELL_CHOICES,
        'default_cell': DEFAULT_CELL,
        'today': date.today().strftime('%Y-%m-%d'),
    }


@app.get('/snapshot')
async def snapshot(inst: str = Query(...), date: Optional[str] = None,
                   tf: str = '5min', cell: Optional[float] = None):
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    if tf not in TF_CHOICES:
        return JSONResponse({'error': f'unknown tf: {tf}'}, status_code=400)
    day = date or datetime.now().strftime('%Y-%m-%d')
    cs  = float(cell) if cell else DEFAULT_CELL[inst]
    snap = _build_snapshot(inst, day, tf, cs)
    return JSONResponse(snap)


# ─── Depth (resting order book) ──────────────────────────────────────────────
def _depth_path(inst: str, day: str) -> pathlib.Path:
    return CACHE_DIR / f'depth_{inst}_{day.replace("-","")}.csv'


def _latest_depth(inst: str, day: str) -> dict:
    """Return the most recent 10-row depth snapshot for inst on day, as
    {'ts_ms': int, 'bids': [(price, qty), ...5], 'asks': [(price, qty), ...5]}.
    Reads only the tail of the CSV for efficiency."""
    p = _depth_path(inst, day)
    if not p.exists():
        return {'ts_ms': 0, 'bids': [], 'asks': []}
    size = p.stat().st_size
    # last ~4 KB — comfortably contains the latest snapshot (10 rows × ~80 B)
    with p.open('rb') as f:
        f.seek(max(0, size - 4096))
        tail = f.read().decode('utf-8', errors='ignore')
    lines = tail.strip().split('\n')
    if not lines:
        return {'ts_ms': 0, 'bids': [], 'asks': []}
    # last row first; find ts_ms of latest snapshot
    latest_ts = None
    snap_rows = []
    for line in reversed(lines):
        parts = line.split(',')
        if len(parts) != 6:
            continue
        try:
            ts_ms = int(parts[0])
        except ValueError:
            continue
        if latest_ts is None:
            latest_ts = ts_ms
        if ts_ms != latest_ts:
            break
        snap_rows.append(parts)
    bids, asks = [], []
    for r in snap_rows:
        ts_ms, inst_r, lvl, side, price, qty = r
        try:
            lvl_i = int(lvl); p_f = float(price); q_f = float(qty)
        except ValueError:
            continue
        (bids if side == 'BID' else asks).append((lvl_i, p_f, q_f))
    bids.sort(key=lambda x: x[0])
    asks.sort(key=lambda x: x[0])
    return {
        'ts_ms': latest_ts or 0,
        'bids': [{'level': l, 'price': p, 'qty': q} for l, p, q in bids],
        'asks': [{'level': l, 'price': p, 'qty': q} for l, p, q in asks],
    }


@app.get('/depth')
async def depth(inst: str = Query(...), date: Optional[str] = None):
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    day = date or datetime.now().strftime('%Y-%m-%d')
    return JSONResponse(_latest_depth(inst, day))


# ─── DOM profile — session-averaged resting qty per price level ──────────────
def _build_dom_profile(inst: str, day: str, cell_size: float,
                       lookback_min: Optional[int] = None) -> dict:
    """Aggregate depth CSV into per-cell resting statistics.

    Returns {cells: [{cell, mean_bid_qty, mean_ask_qty, max_bid_qty,
                       max_ask_qty, bid_persistence, ask_persistence}, ...],
             n_snapshots: int}
    Persistence is fraction of snapshots in which that price cell appeared
    on the given side (0..1). High persistence + high mean qty = a real wall.
    """
    empty = {'cells': [], 'n_snapshots': 0}
    p = _depth_path(inst, day)
    if not p.exists():
        return empty
    try:
        df = pd.read_csv(p)
    except Exception:
        return empty
    if df.empty:
        return empty

    df = df.drop_duplicates(subset=['ts_ms', 'level', 'side'], keep='last')
    if lookback_min and lookback_min > 0:
        cutoff = int(df['ts_ms'].max() - lookback_min * 60_000)
        df = df[df['ts_ms'] >= cutoff]

    n_snaps = int(df['ts_ms'].nunique())
    if n_snaps == 0:
        return empty

    df['price_cell'] = (df['price'] / cell_size).round() * cell_size

    bid = df[df['side'] == 'BID'].groupby('price_cell').agg(
        bid_count   =('ts_ms', 'nunique'),
        mean_bid_qty=('qty',    'mean'),
        max_bid_qty =('qty',    'max'),
    )
    ask = df[df['side'] == 'ASK'].groupby('price_cell').agg(
        ask_count   =('ts_ms', 'nunique'),
        mean_ask_qty=('qty',    'mean'),
        max_ask_qty =('qty',    'max'),
    )
    out = pd.concat([bid, ask], axis=1).fillna(0).reset_index() \
            .rename(columns={'price_cell': 'cell'})
    out['bid_persistence'] = (out['bid_count'] / n_snaps).clip(0, 1)
    out['ask_persistence'] = (out['ask_count'] / n_snaps).clip(0, 1)
    out = out.sort_values('cell')

    cells = []
    for _, r in out.iterrows():
        cells.append({
            'cell':             float(r['cell']),
            'mean_bid_qty':     float(r['mean_bid_qty']),
            'mean_ask_qty':     float(r['mean_ask_qty']),
            'max_bid_qty':      float(r['max_bid_qty']),
            'max_ask_qty':      float(r['max_ask_qty']),
            'bid_persistence':  float(r['bid_persistence']),
            'ask_persistence':  float(r['ask_persistence']),
        })
    return {'cells': cells, 'n_snapshots': n_snaps}


# ─── Composite Volume Profile (prior-day / weekly) from 1m candle history ────
# Built from candles_1m_<INST>.pkl (gapless, ~6 months) rather than ticks
# (which are sparse + holey). Each 1m bar's volume is distributed uniformly
# across the price cells it spanned (low→high) — the standard OHLCV-profile
# approximation. Calendar-aware: "prior day" skips weekends + holidays.
def _prior_trading_days(n: int, before: Optional[date] = None) -> list:
    """Return up to n trading days strictly BEFORE `before` (default today),
    most-recent-last. Bounded scan."""
    from ops.market_calendar import is_trading_day
    before = before or date.today()
    out, d, scanned = [], before - timedelta(days=1), 0
    while len(out) < n and scanned < 40:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1); scanned += 1
    return sorted(out)


def _day_profile(sub: pd.DataFrame, cs: float) -> dict:
    """volume-at-price for one slice of 1m bars. Returns {cell: volume}."""
    prof: dict = defaultdict(float)
    for _, b in sub.iterrows():
        lo = float(b['low']); hi = float(b['high']); vol = float(b['volume'])
        if hi <= 0 or vol <= 0:
            continue
        lo_c = round(lo / cs) * cs
        hi_c = round(hi / cs) * cs
        n_cells = max(1, int(round((hi_c - lo_c) / cs)) + 1)
        v_each = vol / n_cells
        c = lo_c
        for _ in range(n_cells):
            prof[round(c, 2)] += v_each
            c += cs
    return prof


def _classify_profile(prof: dict, cs: float) -> dict:
    """Given {cell: volume}, compute POC, value area (70%), HVN + LVN levels."""
    if not prof:
        return {'cells': [], 'poc': None, 'vah': None, 'val': None,
                'hvn': [], 'lvn': []}
    cells = sorted(prof.items())                       # [(price, vol), ...]
    prices = [c for c, _ in cells]
    vols   = [v for _, v in cells]
    total  = sum(vols)
    vmax   = max(vols)
    poc_i  = vols.index(vmax)
    poc    = prices[poc_i]

    # Value area: expand outward from POC until ≥70% of total volume.
    lo_i = hi_i = poc_i
    acc = vols[poc_i]
    while acc < 0.70 * total and (lo_i > 0 or hi_i < len(vols) - 1):
        take_lo = vols[lo_i - 1] if lo_i > 0 else -1
        take_hi = vols[hi_i + 1] if hi_i < len(vols) - 1 else -1
        if take_hi >= take_lo:
            hi_i += 1; acc += vols[hi_i]
        else:
            lo_i -= 1; acc += vols[lo_i]
    val, vah = prices[lo_i], prices[hi_i]

    # HVN = local peaks ≥ 70th percentile of volume.
    # LVN = local troughs ≤ 25th percentile (rejection gaps).
    import numpy as _np
    p70 = float(_np.percentile(vols, 70))
    p25 = float(_np.percentile(vols, 25))
    hvn, lvn = [], []
    for i in range(len(vols)):
        left  = vols[i - 1] if i > 0 else -1
        right = vols[i + 1] if i < len(vols) - 1 else -1
        if vols[i] >= p70 and vols[i] >= left and vols[i] >= right:
            hvn.append(prices[i])
        if vols[i] <= p25 and vols[i] <= left and vols[i] <= right:
            lvn.append(prices[i])

    return {
        'cells': [{'price': p, 'volume': round(v, 0),
                   'pct': round(v / vmax, 3)} for p, v in cells],
        'poc': poc, 'vah': vah, 'val': val,
        'hvn': hvn, 'lvn': lvn,
    }


def _build_volume_profile(inst: str, scope: str, cell_size: float) -> dict:
    path = CACHE_DIR / f'candles_1m_{inst}.pkl'
    empty = {'inst': inst, 'scope': scope, 'cells': [], 'poc': None,
             'vah': None, 'val': None, 'hvn': [], 'lvn': [],
             'prior_pocs': [], 'days': []}
    if not path.exists():
        return empty
    try:
        df = pickle.load(open(path, 'rb'))
    except Exception:
        return empty
    if df is None or df.empty:
        return empty
    df = df.copy()
    df['d'] = pd.to_datetime(df['ts']).dt.date

    n = 1 if scope == 'prior_day' else 5
    days = _prior_trading_days(n)
    sub = df[df['d'].isin(days)]
    if sub.empty:
        return empty

    prof = _day_profile(sub, cell_size)
    out  = _classify_profile(prof, cell_size)

    # Per-day POCs (for naked-POC marking on the client). A POC is "naked"
    # if today's price action hasn't traded back through it yet — the client
    # decides that using the live price, we just hand over the candidates.
    prior_pocs = []
    for d in days:
        dprof = _day_profile(df[df['d'] == d], cell_size)
        if dprof:
            dpoc = max(dprof.items(), key=lambda kv: kv[1])[0]
            prior_pocs.append({'date': str(d), 'poc': dpoc})

    out.update({'inst': inst, 'scope': scope,
                'days': [str(d) for d in days],
                'prior_pocs': prior_pocs})
    return out


@app.get('/volume_profile')
async def volume_profile(inst: str = Query(...),
                          scope: str = 'prior_day',
                          cell: Optional[float] = None):
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    if scope not in ('prior_day', 'week'):
        scope = 'prior_day'
    cs = float(cell) if cell else DEFAULT_CELL[inst]
    return JSONResponse(_build_volume_profile(inst, scope, cs))


@app.get('/dom_profile')
async def dom_profile(inst: str = Query(...), date: Optional[str] = None,
                       cell: Optional[float] = None,
                       lookback_min: Optional[int] = None):
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    day = date or datetime.now().strftime('%Y-%m-%d')
    cs  = float(cell) if cell else DEFAULT_CELL[inst]
    return JSONResponse(_build_dom_profile(inst, day, cs, lookback_min))


# ─── Unified positioning view ────────────────────────────────────────────────
# Synthesises 4 independent positioning signals into one normalised view:
#   FLOW         — recent footprint CVD (where the aggressors are)
#   RESTING      — DOM imbalance + nearest wall direction (where the walls are)
#   INSTITUTIONS — option_flow conviction (where the institutions are)
#   MACRO        — news_signal direction × confidence (macro tilt)
#
# Each component returns {value ∈ [-1, +1], dir ∈ {-1, 0, +1}, label, ...}.
# Composite is an equal-weighted mean (placeholder until the cross-feature
# regression has enough data to estimate real weights).

FLOW_LOOKBACK_MIN = 5       # footprint CVD window (tightened from 15 → 5 for
                            # responsiveness — 15-min averaged out everything)
FLOW_THRESHOLD    = 0.10    # |normalised CVD| above this triggers a direction
REST_THRESHOLD    = 0.10    # |top-5 imbalance| above this triggers a direction


def _positioning_flow(inst: str, day: str) -> dict:
    """Compute normalised footprint flow over the last FLOW_LOOKBACK_MIN.
    `updated_ms` is the freshest tick ts_ms — lets the UI show staleness."""
    p = _tick_path(inst, day)
    if not p.exists():
        return {'value': 0.0, 'dir': 0, 'label': 'no data', 'n_ticks': 0,
                'updated_ms': 0, 'window_min': FLOW_LOOKBACK_MIN}
    try:
        df = pd.read_csv(p)
    except Exception:
        return {'value': 0.0, 'dir': 0, 'label': 'err', 'n_ticks': 0,
                'updated_ms': 0, 'window_min': FLOW_LOOKBACK_MIN}
    if df.empty:
        return {'value': 0.0, 'dir': 0, 'label': 'no data', 'n_ticks': 0,
                'updated_ms': 0, 'window_min': FLOW_LOOKBACK_MIN}
    latest_ms = int(df['ts_ms'].max())
    cutoff_ms = latest_ms - FLOW_LOOKBACK_MIN * 60_000
    win = df[df['ts_ms'] >= cutoff_ms]
    if win.empty:
        return {'value': 0.0, 'dir': 0, 'label': 'no data', 'n_ticks': 0,
                'updated_ms': latest_ms, 'window_min': FLOW_LOOKBACK_MIN}
    sign = win['side'].map({'BUY': 1, 'SELL': -1}).fillna(0).astype(int)
    signed_qty = (win['qty'] * sign).sum()
    total_qty  = win['qty'].sum()
    val = float(signed_qty / total_qty) if total_qty > 0 else 0.0
    direction = 1 if val > FLOW_THRESHOLD else (-1 if val < -FLOW_THRESHOLD else 0)
    label = 'buy agg' if direction == 1 else ('sell agg' if direction == -1 else 'balanced')
    return {'value': round(val, 3), 'dir': direction, 'label': label,
            'n_ticks': int(len(win)),
            'updated_ms': latest_ms, 'window_min': FLOW_LOOKBACK_MIN}


def _positioning_resting(inst: str, day: str) -> dict:
    """Combine live top-5 imbalance with nearest-wall direction."""
    d = _latest_depth(inst, day)
    bid_sum = sum(b['qty'] for b in d.get('bids', []))
    ask_sum = sum(a['qty'] for a in d.get('asks', []))
    total = bid_sum + ask_sum
    if total == 0:
        return {'value': 0.0, 'dir': 0, 'label': 'no data',
                'bid_qty': 0, 'ask_qty': 0, 'updated_ms': int(d.get('ts_ms', 0))}
    imb = (bid_sum - ask_sum) / total
    direction = 1 if imb > REST_THRESHOLD else (-1 if imb < -REST_THRESHOLD else 0)
    label = 'bid heavy' if direction == 1 else \
            ('ask heavy' if direction == -1 else 'balanced')
    # Nearest top-5 wall (largest single resting size)
    best_bid_q = max((b['qty'] for b in d.get('bids', [])), default=0)
    best_ask_q = max((a['qty'] for a in d.get('asks', [])), default=0)
    return {'value': round(imb, 3), 'dir': direction, 'label': label,
            'bid_qty': float(bid_sum), 'ask_qty': float(ask_sum),
            'best_bid_qty': float(best_bid_q),
            'best_ask_qty': float(best_ask_q),
            'updated_ms': int(d.get('ts_ms', 0))}


def _positioning_institutions(inst: str) -> dict:
    """Read option_flow conviction from v3/cache/option_flow_<INST>.json.
    Refresh cadence: tied to option_flow_daemon (typically ~1 / min)."""
    p = CACHE_DIR / f'option_flow_{inst}.json'
    if not p.exists():
        return {'value': 0.0, 'dir': 0, 'label': 'no data', 'conv': 0.0,
                'updated_ms': 0}
    try:
        d = json.loads(p.read_text())
        # ts in file is ISO with tz; fall back to file mtime
        ts_iso = d.get('ts')
        if ts_iso:
            updated_ms = int(datetime.fromisoformat(ts_iso).timestamp() * 1000)
        else:
            updated_ms = int(p.stat().st_mtime * 1000)
    except Exception:
        return {'value': 0.0, 'dir': 0, 'label': 'err', 'conv': 0.0,
                'updated_ms': 0}
    conv = float(d.get('conviction', 0.0))
    band = int(d.get('conv_band', 0))
    direction = int(d.get('direction', 0)) or band
    val = max(-1.0, min(1.0, conv / 3.0))
    label = ('bull' if direction == 1 else ('bear' if direction == -1 else 'flat'))
    return {'value': round(val, 3), 'dir': direction, 'label': label,
            'conv': round(conv, 3), 'conv_band': band,
            'top_strike': (d.get('top_strikes') or [{}])[0].get('strike'),
            'top_state':  (d.get('top_strikes') or [{}])[0].get('state'),
            'updated_ms': updated_ms}


def _positioning_macro() -> dict:
    """Read news_signal.json — score × confidence as magnitude.
    Refresh cadence: news/runner.py writes this on each scoring cycle (minutes)."""
    p = CACHE_DIR / 'news_signal.json'
    if not p.exists():
        return {'value': 0.0, 'dir': 0, 'label': 'no data', 'score': 0.0,
                'updated_ms': 0}
    try:
        d = json.loads(p.read_text())
        updated_ms = int(p.stat().st_mtime * 1000)
    except Exception:
        return {'value': 0.0, 'dir': 0, 'label': 'err', 'score': 0.0,
                'updated_ms': 0}
    score = float(d.get('score', 0.0))
    conf  = float(d.get('confidence', 0.0))
    val   = max(-1.0, min(1.0, score * conf))
    direction = 1 if val > 0.1 else (-1 if val < -0.1 else 0)
    label = 'bull' if direction == 1 else \
            ('bear' if direction == -1 else 'neutral')
    return {'value': round(val, 3), 'dir': direction, 'label': label,
            'score': round(score, 3), 'confidence': round(conf, 3),
            'n_clusters': int(d.get('n_clusters', 0)),
            'updated_ms': updated_ms}


# ─── Classic floor pivots — prior trading day's OHLC ────────────────────────
def _prior_day_ohlc(inst: str, today: date) -> Optional[dict]:
    """Find the most recent COMPLETED trading day before `today` and return
    its 09:15-15:30 OHLC from the 1m candle cache."""
    path = CACHE_DIR / f'candles_1m_{inst}.pkl'
    if not path.exists():
        return None
    try:
        import pickle
        df = pickle.load(open(path, 'rb'))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df['date'] = pd.to_datetime(df['ts']).dt.date
    # Most recent date strictly before today
    days = sorted(d for d in df['date'].unique() if d < today)
    if not days:
        return None
    prior = days[-1]
    sub = df[df['date'] == prior]
    if sub.empty:
        return None
    return {
        'date':  str(prior),
        'open':  float(sub['open'].iloc[0]),
        'high':  float(sub['high'].max()),
        'low':   float(sub['low'].min()),
        'close': float(sub['close'].iloc[-1]),
    }


def _classic_pivots(h: float, l: float, c: float) -> dict:
    """Standard floor pivots from prior day's H/L/C."""
    p = (h + l + c) / 3.0
    rng = h - l
    return {
        'P':  round(p,        2),
        'R1': round(2*p - l,  2),
        'S1': round(2*p - h,  2),
        'R2': round(p + rng,  2),
        'S2': round(p - rng,  2),
        'R3': round(h + 2*(p - l), 2),
        'S3': round(l - 2*(h - p), 2),
    }


@app.get('/pivots')
async def pivots(inst: str = Query(...), date: Optional[str] = None):
    """Classic floor pivots from prior trading day's OHLC."""
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    from datetime import date as _date
    today = _date.fromisoformat(date) if date else _date.today()
    ohlc = _prior_day_ohlc(inst, today)
    if not ohlc:
        return JSONResponse({'error': 'no prior-day data', 'inst': inst})
    piv = _classic_pivots(ohlc['high'], ohlc['low'], ohlc['close'])
    return JSONResponse({
        'inst': inst, 'today': str(today),
        'prior_day': ohlc,
        'pivots': piv,
    })


# Throttle the positioning log writes so a chatty client (or multiple
# browser tabs) doesn't bloat the file. 5 s matches the viewer's poll cadence.
_POS_LOG_MIN_INTERVAL_S = 5.0
_POS_LOG_LAST: dict[str, float] = {}


def _latest_ltp_from_ticks(inst: str, day: str) -> Optional[float]:
    """Return the most recent traded price for `inst` on `day` by reading
    the tail of the tick CSV (cheap, ~4 KB seek-to-end)."""
    p = _tick_path(inst, day)
    if not p.exists():
        return None
    size = p.stat().st_size
    try:
        with p.open('rb') as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode('utf-8', errors='ignore')
        lines = [ln for ln in tail.split('\n') if ',' in ln]
        if not lines:
            return None
        # Last row's `price` column is index 2 in the canonical schema:
        # ts_ms,inst,price,qty,side,rule,bid,ask,bid_qty,ask_qty,spread,cum_volume
        last = lines[-1].split(',')
        return float(last[2])
    except Exception:
        return None


def _log_positioning(inst: str, day: str, payload: dict) -> None:
    """Append one JSON line to v3/cache/positioning_<INST>_<DATE>.ndjson.
    Throttled to once per _POS_LOG_MIN_INTERVAL_S per (inst, day) so that
    multiple browser tabs or chatty pollers don't bloat the file."""
    now = time.time()
    key = f'{inst}|{day}'
    last = _POS_LOG_LAST.get(key, 0.0)
    if now - last < _POS_LOG_MIN_INTERVAL_S:
        return
    _POS_LOG_LAST[key] = now
    fp = CACHE_DIR / f'positioning_{inst}_{day.replace("-","")}.ndjson'
    try:
        with open(fp, 'a') as f:
            f.write(json.dumps(payload, separators=(',', ':')) + '\n')
    except Exception as e:
        log.warning('positioning log write %s: %s', fp.name, e)


@app.get('/positioning')
async def positioning(inst: str = Query(...), date: Optional[str] = None):
    """Return the unified positioning snapshot for one instrument and
    append it to the per-day NDJSON log for later forward-return analysis."""
    if inst not in INSTRUMENTS:
        return JSONResponse({'error': f'unknown inst: {inst}'}, status_code=400)
    day = date or datetime.now().strftime('%Y-%m-%d')

    flow  = _positioning_flow(inst, day)
    rest  = _positioning_resting(inst, day)
    inst_ = _positioning_institutions(inst)
    macro = _positioning_macro()

    # Equal-weighted composite (placeholder — swap to regression weights when
    # research/footprint_correlation outputs them).
    vals  = [flow['value'], rest['value'], inst_['value'], macro['value']]
    comp  = sum(vals) / len(vals)
    comp_dir = 1 if comp > 0.10 else (-1 if comp < -0.10 else 0)
    comp_label = ('bullish' if comp_dir == 1 else
                  ('bearish' if comp_dir == -1 else 'mixed'))
    composite = {
        'value': round(comp, 3), 'dir': comp_dir, 'label': comp_label,
        'aligned': (flow['dir'] == rest['dir'] == inst_['dir'] == macro['dir']
                    and flow['dir'] != 0),
    }
    body = {
        'inst': inst, 'date': day,
        'flow': flow, 'resting': rest,
        'institutions': inst_, 'macro': macro,
        'composite': composite,
    }

    # Persist for research — every 5 s, append a compact NDJSON line with
    # the LTP anchor so forward-return analysis can compute "did price move
    # in direction(composite) over next N min" later.
    now = datetime.now()
    ltp = _latest_ltp_from_ticks(inst, day)
    log_row = {
        'ts_iso': now.isoformat(timespec='seconds'),
        'ts_ms':  int(now.timestamp() * 1000),
        'inst':   inst,
        'ltp':    ltp,
        'flow':         flow,
        'resting':      rest,
        'institutions': inst_,
        'macro':        macro,
        'composite':    composite,
    }
    _log_positioning(inst, day, log_row)

    return JSONResponse(body)


# ─── WebSocket tail-streamer ─────────────────────────────────────────────────
@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket, inst: str = Query(...),
                      date_: Optional[str] = Query(None, alias='date'),
                      poll_ms: int = Query(500)):
    """Stream new tick rows for `inst` on `date_` (default today). Pushes a
    JSON list of new rows every poll_ms when there's anything new."""
    if inst not in INSTRUMENTS:
        await ws.close(code=1008); return
    await ws.accept()
    day = date_ or datetime.now().strftime('%Y-%m-%d')
    log.info("WS open inst=%s day=%s poll=%dms", inst, day, poll_ms)

    # -1 = uninitialised sentinel. _read_ticks resolves this to current EOF
    # on its first call without replaying file history (which would cause
    # client-side double-counting against the snapshot).
    p = _tick_path(inst, day)
    last_byte = p.stat().st_size if p.exists() else -1

    last_depth_ts = 0
    last_tick_wall = time.time()
    stall_notified = False
    try:
        while True:
            await asyncio.sleep(poll_ms / 1000.0)

            # 1) tick tail
            tail, new_byte = _read_ticks(inst, day, last_byte)
            # Always advance the cursor — even on empty reads — so the
            # uninitialised-sentinel case (-1 → current EOF) resolves on the
            # first poll. Otherwise we'd remain at -1 forever and miss every
            # subsequent tick.
            last_byte = new_byte
            if not tail.empty:
                rows = []
                for _, r in tail.iterrows():
                    rows.append({
                        'ts_ms': int(r['ts_ms']),
                        'price': float(r['price']),
                        'qty':   float(r['qty']) if pd.notna(r['qty']) else 0.0,
                        'side':  str(r['side']),
                        'rule':  str(r['rule']),
                        'bid':   float(r['bid']) if pd.notna(r['bid']) else 0.0,
                        'ask':   float(r['ask']) if pd.notna(r['ask']) else 0.0,
                    })
                await ws.send_json({'type': 'ticks', 'inst': inst,
                                    'rows': rows, 'last_byte': last_byte})
                last_tick_wall = time.time()
                if stall_notified:
                    await ws.send_json({'type': 'resume', 'inst': inst,
                                        'wall_ms': int(last_tick_wall * 1000)})
                    stall_notified = False
            else:
                # Surface a stall warning ONCE per outage, only during market
                # hours (avoid noisy alerts before 09:15 or after 15:35).
                now_wall = time.time()
                stall_age = now_wall - last_tick_wall
                if (not stall_notified and stall_age > WS_STALL_THRESHOLD_SEC
                        and _market_hours_now()):
                    log.warning("WS stall inst=%s age=%.0fs — recorder may "
                                "be reconnecting", inst, stall_age)
                    await ws.send_json({
                        'type': 'stall', 'inst': inst,
                        'stall_age_s': round(stall_age, 1),
                        'since_ms': int(last_tick_wall * 1000),
                    })
                    stall_notified = True

            # 2) depth snapshot — push when ts_ms advances
            d = _latest_depth(inst, day)
            if d['ts_ms'] and d['ts_ms'] != last_depth_ts:
                last_depth_ts = d['ts_ms']
                await ws.send_json({'type': 'depth', 'inst': inst, **d})
    except WebSocketDisconnect:
        log.info("WS closed inst=%s", inst)
    except Exception as e:
        log.warning("WS error inst=%s: %s", inst, e)


# ─── Entrypoint ──────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level='info')


if __name__ == '__main__':
    main()
