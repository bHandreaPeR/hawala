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
from datetime import date, datetime
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('viewer')

app = FastAPI(title='Hawala live footprint')
app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')


# ─── tick file IO ────────────────────────────────────────────────────────────
def _tick_path(inst: str, day: str) -> pathlib.Path:
    return CACHE_DIR / f'ticks_{inst}_{day.replace("-","")}.csv'


def _read_ticks(inst: str, day: str, since_byte: int = 0
                ) -> tuple[pd.DataFrame, int]:
    """Read ticks from byte offset to EOF. Returns (df, new_byte_offset)."""
    p = _tick_path(inst, day)
    if not p.exists():
        return pd.DataFrame(), 0
    size = p.stat().st_size
    if since_byte == 0:
        df = pd.read_csv(p) if size > 0 else pd.DataFrame()
        return df, size
    if since_byte >= size:
        return pd.DataFrame(), since_byte
    # tail-read: skip-rows is the easier route — keeps schema clean
    df = pd.read_csv(p)
    # offset is on bytes, not rows. We can re-read the whole file and dedup
    # against the WS-already-pushed set on the client. For now just re-read
    # the tail by counting lines: read the slice from since_byte to EOF.
    with p.open('rb') as f:
        f.seek(since_byte)
        chunk = f.read().decode('utf-8', errors='ignore')
    # leading line may be partial — drop it
    lines = chunk.split('\n')[1:]
    text = '\n'.join(lines).strip()
    if not text:
        return pd.DataFrame(), size
    from io import StringIO
    cols = list(df.columns) if not df.empty else [
        'ts_ms','inst','price','qty','side','rule','bid','ask',
        'bid_qty','ask_qty','spread','cum_volume']
    try:
        tail = pd.read_csv(StringIO(text), names=cols, header=None)
    except Exception:
        return pd.DataFrame(), size
    return tail, size


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

    last_byte = 0
    p = _tick_path(inst, day)
    if p.exists():
        last_byte = p.stat().st_size  # start from EOF — only stream NEW

    last_depth_ts = 0
    try:
        while True:
            await asyncio.sleep(poll_ms / 1000.0)

            # 1) tick tail
            tail, new_byte = _read_ticks(inst, day, last_byte)
            if not tail.empty:
                last_byte = new_byte
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
