/* viewer/static/app.js — Live footprint frontend.
 * Bootstraps from /config + /snapshot, then patches via WebSocket /ws.
 *
 * State model:
 *   state.snap       — full snapshot from server
 *   state.cells_idx  — { "bucket|cell" → cell-row obj } for fast updates
 *   state.candles_idx— { bucket → candle obj }
 *   state.pending    — patch counter for batched relayout (~10 Hz max)
 *
 * Tick flow:
 *   server polls ticks CSV every 500ms, broadcasts new rows via /ws
 *   onmessage: classify each tick into its bucket+cell, mutate state, mark dirty
 *   redraw on requestAnimationFrame throttle
 */
"use strict";

const TF_MS = {'1min': 60000, '3min': 180000, '5min': 300000, '15min': 900000};
const IMB_MULT = 3, IMB_MIN_TICKS = 4;

const state = {
  cfg: null, inst: null, tf: '5min', cell_size: 5, date: null,
  paused: false, autoscroll: true,
  snap: null,
  cells_idx: new Map(),    // key = `${bucket_ms}|${cell}`
  candles_idx: new Map(),  // key = bucket_ms
  ws: null, ws_url: null,
  dirty: false, n_ticks: 0,
  last_byte: 0,
  depth: {ts_ms: 0, bids: [], asks: []},   // live top-5 resting orders
  depth_dirty: false,
};

// ─── DOM bootstrap ──────────────────────────────────────────────────────────
async function bootstrap () {
  const cfg = await fetch('/config').then(r => r.json());
  state.cfg = cfg;

  const $inst = document.getElementById('inst');
  cfg.instruments.forEach(i => {
    const o = document.createElement('option');
    o.value = i; o.textContent = i;
    $inst.appendChild(o);
  });
  state.inst = cfg.instruments[0];
  $inst.value = state.inst;

  const $date = document.getElementById('date');
  $date.value = cfg.today;
  state.date = cfg.today;

  rebuildCellChoices();

  $inst.addEventListener('change', () => {
    state.inst = $inst.value;
    rebuildCellChoices();
    fullReload();
  });
  document.getElementById('tf').addEventListener('change', e => {
    state.tf = e.target.value; fullReload();
  });
  document.getElementById('cell').addEventListener('change', e => {
    state.cell_size = parseFloat(e.target.value); fullReload();
  });
  $date.addEventListener('change', e => { state.date = e.target.value; fullReload(); });

  document.getElementById('pause').addEventListener('click', e => {
    state.paused = !state.paused;
    e.target.textContent = state.paused ? '▶ Resume' : '⏸ Pause';
    e.target.classList.toggle('btn-active', state.paused);
  });
  document.getElementById('autoscroll').addEventListener('click', e => {
    state.autoscroll = !state.autoscroll;
    e.target.classList.toggle('btn-active', state.autoscroll);
  });
  document.getElementById('reload').addEventListener('click', fullReload);

  await fullReload();
  startRedrawLoop();
}

function rebuildCellChoices () {
  const $cell = document.getElementById('cell');
  $cell.innerHTML = '';
  const choices = state.cfg.cells[state.inst] || [5, 10];
  const def = state.cfg.default_cell[state.inst] || choices[Math.floor(choices.length/2)];
  choices.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = `${v}pt`;
    if (v === def) o.selected = true;
    $cell.appendChild(o);
  });
  state.cell_size = parseFloat($cell.value);
}

// ─── Full reload (snapshot + reopen WS) ─────────────────────────────────────
async function fullReload () {
  setStatus('loading snapshot…');
  if (state.ws) { try { state.ws.close(); } catch(e){} state.ws = null; }
  const q = new URLSearchParams({inst: state.inst, date: state.date,
                                 tf: state.tf, cell: state.cell_size});
  state.snap = await fetch('/snapshot?' + q).then(r => r.json());
  state.n_ticks = state.snap.n_ticks || 0;
  rebuildIndex();
  redrawAll();
  setStatus('live ✓');
  openWS();
}

function rebuildIndex () {
  state.cells_idx.clear();
  state.candles_idx.clear();
  for (const c of state.snap.cells) {
    state.cells_idx.set(`${c.bucket}|${c.cell}`, c);
  }
  for (const k of state.snap.candles) {
    state.candles_idx.set(k.bucket, k);
  }
}

// ─── WebSocket — tail new ticks, patch state ────────────────────────────────
function openWS () {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws_url = `${proto}://${location.host}/ws?inst=${state.inst}&date=${state.date}`;
  const ws = new WebSocket(state.ws_url);
  state.ws = ws;
  document.getElementById('ws_state').textContent = 'ws: connecting';
  ws.addEventListener('open',  () => {
    document.getElementById('ws_state').textContent = 'ws: ✓ open';
  });
  ws.addEventListener('close', () => {
    document.getElementById('ws_state').textContent = 'ws: closed (retrying)';
    if (!state.paused) setTimeout(openWS, 1500);
  });
  ws.addEventListener('error', () => {
    document.getElementById('ws_state').textContent = 'ws: err';
  });
  ws.addEventListener('message', ev => {
    if (state.paused) return;
    const m = JSON.parse(ev.data);
    // Defence 1: drop any message whose inst doesn't match the active symbol.
    // Race during NIFTY ⇄ BANKNIFTY switches can otherwise pollute state with
    // cross-instrument cells (causes a 54k cell to appear in a NIFTY chart).
    if (m.inst && m.inst !== state.inst) return;
    if (m.type === 'ticks') applyTicks(m.rows);
    if (m.type === 'depth') {
      state.depth = {ts_ms: m.ts_ms, bids: m.bids, asks: m.asks};
      state.depth_dirty = true;
    }
  });

  // Initial depth fetch — server pushes only on new ts; seed via REST.
  fetch(`/depth?inst=${state.inst}&date=${state.date}`)
    .then(r => r.json())
    .then(d => { state.depth = d; state.depth_dirty = true; })
    .catch(() => {});
}

// ─── Apply a batch of ticks to local state ──────────────────────────────────
function applyTicks (rows) {
  const tf_ms = TF_MS[state.tf];
  const cs    = state.cell_size;
  // Defence 2: per-row sanity bound. Compute the median price of currently-
  // known candles; drop any row whose price is >10% off — that's cross-
  // instrument leakage, not a real outlier (NIFTY itself doesn't move 10%
  // intraday).
  let priceAnchor = 0;
  if (state.candles_idx.size > 0) {
    const closes = [...state.candles_idx.values()].map(k => k.close);
    closes.sort((a,b) => a - b);
    priceAnchor = closes[Math.floor(closes.length / 2)];   // median close
  }

  for (const r of rows) {
    if (priceAnchor > 0 && Math.abs(r.price - priceAnchor) / priceAnchor > 0.10) {
      continue;     // silently drop — wrong-instrument or corrupt row
    }
    const bucket = Math.floor(r.ts_ms / tf_ms) * tf_ms;
    const cell   = Math.round(r.price / cs) * cs;
    const key = `${bucket}|${cell}`;
    let c = state.cells_idx.get(key);
    if (!c) {
      c = { bucket, cell, buy_ticks:0, sell_ticks:0, buy_qty:0, sell_qty:0,
            total_ticks:0, total_qty:0, imbalance:null };
      state.cells_idx.set(key, c);
      state.snap.cells.push(c);
    }
    if (r.side === 'BUY')  { c.buy_ticks++;  c.buy_qty  += r.qty; }
    if (r.side === 'SELL') { c.sell_ticks++; c.sell_qty += r.qty; }
    c.total_ticks = c.buy_ticks + c.sell_ticks;
    c.total_qty   = c.buy_qty   + c.sell_qty;
    if (c.total_ticks >= IMB_MIN_TICKS) {
      if (c.buy_ticks  >= IMB_MULT * Math.max(c.sell_ticks, 1)) c.imbalance = 'BUY';
      else if (c.sell_ticks >= IMB_MULT * Math.max(c.buy_ticks, 1)) c.imbalance = 'SELL';
      else c.imbalance = null;
    }

    // update / create candle
    let k = state.candles_idx.get(bucket);
    if (!k) {
      k = { bucket, open: r.price, high: r.price, low: r.price, close: r.price,
            poc: r.price, vah: r.price, val: r.price, delta_qty: 0, cvd_qty: 0 };
      state.candles_idx.set(bucket, k);
      state.snap.candles.push(k);
    }
    k.high  = Math.max(k.high, r.price);
    k.low   = Math.min(k.low,  r.price);
    k.close = r.price;
  }
  state.n_ticks += rows.length;
  document.getElementById('tick_counter').textContent =
    `ticks: ${state.n_ticks}`;
  document.getElementById('last_update').textContent =
    `last: ${new Date().toLocaleTimeString()}`;
  state.dirty = true;
}

// ─── Render loop — at most 5 Hz to keep CPU low ─────────────────────────────
function startRedrawLoop () {
  setInterval(() => {
    if (!state.dirty && !state.depth_dirty) return;
    if (state.dirty) recomputePOCs();
    state.dirty = false;
    state.depth_dirty = false;
    redrawAll();
  }, 200);   // 5 Hz
}

function recomputePOCs () {
  // per-bucket POC + delta refresh
  const byBucket = new Map();
  for (const c of state.snap.cells) {
    if (!byBucket.has(c.bucket)) byBucket.set(c.bucket, []);
    byBucket.get(c.bucket).push(c);
  }
  for (const [bk, cs] of byBucket) {
    const cd = state.candles_idx.get(bk);
    if (!cd) continue;
    cd.delta_qty = cs.reduce((a, c) => a + (c.buy_qty - c.sell_qty), 0);
    const useQty = cs.some(c => c.total_qty > 0);
    let best = cs[0], bestv = useQty ? cs[0].total_qty : cs[0].total_ticks;
    for (const c of cs) {
      const v = useQty ? c.total_qty : c.total_ticks;
      if (v > bestv) { best = c; bestv = v; }
    }
    cd.poc = best.cell;
  }
  // cumulative delta in time order
  const sorted = [...state.candles_idx.values()].sort((a,b) => a.bucket - b.bucket);
  let cum = 0;
  for (const k of sorted) { cum += k.delta_qty; k.cvd_qty = cum; }
}

// ─── Drawing — Plotly figure ────────────────────────────────────────────────
function redrawAll () {
  const candles = [...state.candles_idx.values()].sort((a,b) => a.bucket - b.bucket);
  const cells   = state.snap.cells;
  if (candles.length === 0) {
    Plotly.purge('chart');
    Plotly.newPlot('chart',
      [{x:[], y:[], type:'scatter'}],
      {title:'no data yet — waiting for ticks…',
       paper_bgcolor:'#fafafa', plot_bgcolor:'#fff'},
      {responsive:true});
    return;
  }

  const tf_ms  = TF_MS[state.tf];
  const half_w = tf_ms * 0.40;
  const body_w = tf_ms * 0.09;
  const cs     = state.cell_size;

  // per-bucket maxima for relative shading
  const maxes = new Map();   // bucket → {buyMax, sellMax}
  for (const c of cells) {
    const m = maxes.get(c.bucket) || {buy:1, sell:1};
    if (c.buy_qty  > m.buy)  m.buy  = c.buy_qty;
    if (c.sell_qty > m.sell) m.sell = c.sell_qty;
    if (c.buy_qty === 0 && c.sell_qty === 0) {
      if (c.buy_ticks  > m.buy)  m.buy  = c.buy_ticks;
      if (c.sell_ticks > m.sell) m.sell = c.sell_ticks;
    }
    maxes.set(c.bucket, m);
  }

  const useQty = cells.some(c => c.total_qty > 0);
  const shapes = [], annos = [];

  // value-area band per candle
  for (const c of candles) {
    shapes.push({
      type:'rect', xref:'x', yref:'y',
      x0:c.bucket - half_w, x1:c.bucket + half_w,
      y0:c.val - cs/2,      y1:c.vah + cs/2,
      fillcolor:'rgba(46,125,50,0.06)', line:{width:0}, layer:'below',
    });
  }

  // cells
  for (const c of cells) {
    const m = maxes.get(c.bucket) || {buy:1, sell:1};
    const sv = useQty ? c.sell_qty : c.sell_ticks;
    const bv = useQty ? c.buy_qty  : c.buy_ticks;
    const s_int = sv / (m.sell || 1);
    const b_int = bv / (m.buy  || 1);
    const sCol = sv > 0 ? `rgba(239,83,80,${(0.10 + 0.75*s_int).toFixed(2)})`
                        : 'rgba(239,83,80,0.06)';
    const bCol = bv > 0 ? `rgba(38,166,154,${(0.10 + 0.75*b_int).toFixed(2)})`
                        : 'rgba(38,166,154,0.06)';

    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket - half_w, x1:c.bucket - body_w,
      y0:c.cell - cs/2,     y1:c.cell + cs/2,
      fillcolor:sCol, line:{width:0.3, color:'rgba(0,0,0,0.18)'}});
    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket + body_w, x1:c.bucket + half_w,
      y0:c.cell - cs/2,     y1:c.cell + cs/2,
      fillcolor:bCol, line:{width:0.3, color:'rgba(0,0,0,0.18)'}});

    if (c.imbalance === 'BUY') {
      shapes.push({type:'rect', xref:'x', yref:'y',
        x0:c.bucket + body_w, x1:c.bucket + half_w,
        y0:c.cell - cs/2,     y1:c.cell + cs/2,
        fillcolor:'rgba(0,0,0,0)', line:{width:2, color:'#ffb300'}});
    } else if (c.imbalance === 'SELL') {
      shapes.push({type:'rect', xref:'x', yref:'y',
        x0:c.bucket - half_w, x1:c.bucket - body_w,
        y0:c.cell - cs/2,     y1:c.cell + cs/2,
        fillcolor:'rgba(0,0,0,0)', line:{width:2, color:'#ffb300'}});
    }

    annos.push({
      x: c.bucket - (half_w + body_w)/2, y: c.cell,
      xref:'x', yref:'y', showarrow:false,
      text: c.imbalance === 'SELL' ? `<b>${fmtNum(sv)}</b>` : fmtNum(sv),
      font:{size:9, color: sv > 0 ? '#b71c1c' : '#999'},
    });
    annos.push({
      x: c.bucket + (half_w + body_w)/2, y: c.cell,
      xref:'x', yref:'y', showarrow:false,
      text: c.imbalance === 'BUY'  ? `<b>${fmtNum(bv)}</b>` : fmtNum(bv),
      font:{size:9, color: bv > 0 ? '#0d6e63' : '#999'},
    });
  }

  // candle wick + body + POC ring
  for (const c of candles) {
    const up = c.close >= c.open;
    const col = up ? '#26a69a' : '#ef5350';
    shapes.push({type:'line', xref:'x', yref:'y',
      x0:c.bucket, x1:c.bucket, y0:c.low, y1:c.high,
      line:{color:col, width:1.2}});
    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket - body_w, x1:c.bucket + body_w,
      y0:Math.min(c.open, c.close), y1:Math.max(c.open, c.close),
      fillcolor:col, line:{width:0}});
    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket - half_w, x1:c.bucket + half_w,
      y0:c.poc - cs/2, y1:c.poc + cs/2,
      fillcolor:'rgba(0,0,0,0)', line:{width:1.5, color:'#fdd835'}});
  }

  const xRange = [candles[0].bucket - tf_ms*0.6,
                  candles[candles.length-1].bucket + tf_ms*0.6];

  // ── Right-pane #1 — Live DOM (resting top-5 bids/asks) ──────────────────
  const dom = state.depth || {bids:[], asks:[]};
  const domBidPx = dom.bids.map(b => b.price);
  const domBidQ  = dom.bids.map(b => b.qty);
  const domAskPx = dom.asks.map(a => a.price);
  const domAskQ  = dom.asks.map(a => -a.qty);    // negative → left of axis
  const domMax   = Math.max(1, ...domBidQ, ...dom.asks.map(a => a.qty));
  // colour intensity ∝ size; level-1 (best) gets full saturation
  const bidColors = dom.bids.map(b =>
    `rgba(38,166,154,${(0.30 + 0.65 * b.qty/domMax).toFixed(2)})`);
  const askColors = dom.asks.map(a =>
    `rgba(239,83,80,${(0.30 + 0.65 * a.qty/domMax).toFixed(2)})`);
  // text labels — show qty on each bar
  const bidLabels = dom.bids.map(b => `${b.qty}`);
  const askLabels = dom.asks.map(a => `${a.qty}`);

  // ── Right-pane #2 — Session profile (rebuild from cells) ────────────────
  const profByCell = new Map();
  for (const c of cells) {
    const p = profByCell.get(c.cell) || {cell:c.cell, buy_qty:0, sell_qty:0,
                                          buy_ticks:0, sell_ticks:0};
    p.buy_qty   += c.buy_qty;
    p.sell_qty  += c.sell_qty;
    p.buy_ticks += c.buy_ticks;
    p.sell_ticks+= c.sell_ticks;
    profByCell.set(c.cell, p);
  }
  const prof = [...profByCell.values()].sort((a,b) => a.cell - b.cell);
  const profUseQty = prof.some(p => p.buy_qty > 0 || p.sell_qty > 0);
  const profBuy  = prof.map(p => profUseQty ? p.buy_qty  : p.buy_ticks);
  const profSell = prof.map(p => profUseQty ? -p.sell_qty : -p.sell_ticks);
  const profY    = prof.map(p => p.cell);

  // Bottom-pane delta histogram
  const dx = candles.map(k => k.bucket);
  const dy = candles.map(k => k.delta_qty);
  const dcol = dy.map(v => v >= 0 ? '#26a69a' : '#ef5350');

  const layout = {
    paper_bgcolor:'#fafafa', plot_bgcolor:'#fff',
    margin:{l:55, r:15, t:25, b:30},
    showlegend:false,
    // domains: main=72%, session profile=12%, live DOM=14% (right edge)
    xaxis:  {type:'date', domain:[0, 0.72], range:xRange, title:''},
    yaxis:  {domain:[0.27, 1], title:'price', side:'left'},
    xaxis2: {domain:[0.73, 0.84], anchor:'y2', showticklabels:false, title:'session vol'},
    yaxis2: {domain:[0.27, 1], matches:'y', showticklabels:false},
    xaxis3: {type:'date', domain:[0, 1], anchor:'y3', range:xRange, title:''},
    yaxis3: {domain:[0, 0.22], title:'Δ qty'},
    xaxis4: {domain:[0.86, 1.0], anchor:'y4', showticklabels:false, title:'live DOM'},
    yaxis4: {domain:[0.27, 1], matches:'y', showticklabels:false},
    shapes, annotations: annos,
    barmode:'overlay',
    uirevision: state.autoscroll ? undefined : 'pin',  // preserves zoom unless autoscroll
  };

  const traces = [
    // anchor for main pane axis type
    {x:[candles[0].bucket, candles[candles.length-1].bucket],
     y:[Math.min(...candles.map(c=>c.low))  - 1.5*cs,
        Math.max(...candles.map(c=>c.high)) + 1.5*cs],
     mode:'markers', marker:{size:0.1, color:'rgba(0,0,0,0)'},
     hoverinfo:'skip', xaxis:'x', yaxis:'y'},
    // session profile (pane #2)
    {x: profSell, y: profY, type:'bar', orientation:'h',
     xaxis:'x2', yaxis:'y2',
     marker:{color:'rgba(239,83,80,0.85)'},
     hovertemplate:'price=%{y}<br>sell=%{x:.0f}<extra></extra>'},
    {x: profBuy,  y: profY, type:'bar', orientation:'h',
     xaxis:'x2', yaxis:'y2',
     marker:{color:'rgba(38,166,154,0.85)'},
     hovertemplate:'price=%{y}<br>buy=%{x:.0f}<extra></extra>'},
    // delta histogram (bottom pane)
    {x: dx, y: dy, type:'bar', marker:{color: dcol},
     xaxis:'x3', yaxis:'y3',
     hovertemplate:'%{x}<br>Δ=%{y:.0f}<extra></extra>'},
    // live DOM (pane #3) — bids on right side of axis, asks on left
    {x: domBidQ, y: domBidPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4',
     marker:{color: bidColors,
             line:{color:'rgba(0,0,0,0.2)', width:0.5}},
     text: bidLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:10, color:'#0d6e63'},
     hovertemplate:'BID %{y}<br>qty=%{x}<extra></extra>'},
    {x: domAskQ, y: domAskPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4',
     marker:{color: askColors,
             line:{color:'rgba(0,0,0,0.2)', width:0.5}},
     text: askLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:10, color:'#b71c1c'},
     hovertemplate:'ASK %{y}<br>qty=%{x:.0f}<extra></extra>'},
  ];

  Plotly.react('chart', traces, layout, {responsive:true, displaylogo:false});

  document.getElementById('meta').textContent =
    `${candles.length} candles · ${cells.length} cells · tf=${state.tf} cell=${state.cell_size}`;
}

function fmtNum (v) {
  if (!v) return '0';
  if (v >= 1000) return (v/1000).toFixed(1) + 'k';
  return String(Math.round(v));
}

function setStatus (txt, err=false) {
  const $s = document.getElementById('status');
  $s.textContent = txt;
  $s.classList.toggle('err', !!err);
}

document.addEventListener('DOMContentLoaded', bootstrap);
