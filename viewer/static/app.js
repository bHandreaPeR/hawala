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

// At zoom 1.0 the chart shows ALL available candles (no pre-09:15 padding)
// and Plotly auto-fits the y-range to visible-candle highs/lows. Zoom > 1.0
// progressively focuses on recent activity around the latest close.
const X_TICK_LABEL_EVERY = {'1min': 5, '3min': 2, '5min': 1, '15min': 1}; // every Nth gridline gets a label

// Zoom factors: zoom_x > 1 = fewer candles visible (zoomed IN on time).
//                zoom_y > 1 = fewer cells visible (zoomed IN on price).
// Independent per-axis, persisted to localStorage so reloads keep the view.
const ZOOM_STEPS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0];
const _stored = (k, d) => {
  const v = parseFloat(localStorage.getItem(k));
  return Number.isFinite(v) && ZOOM_STEPS.includes(v) ? v : d;
};

const state = {
  cfg: null, inst: null, tf: '5min', cell_size: 5, date: null,
  paused: false, autoscroll: true,
  view_rev: 0,        // bumps when a button forces a programmatic view change
  force_range: false, // one-shot: apply computed range on the next redraw
  zoom_x: _stored('hawala_zoom_x', 1.0),
  zoom_y: _stored('hawala_zoom_y', 1.0),
  snap: null,
  cells_idx: new Map(),    // key = `${bucket_ms}|${cell}`
  candles_idx: new Map(),  // key = bucket_ms
  ws: null, ws_url: null, ws_seq: 0,        // ws_seq: ignore msgs from stale sockets
  dirty: false, n_ticks: 0,
  last_byte: 0,
  depth: {ts_ms: 0, bids: [], asks: []},   // live top-5 resting orders
  depth_dirty: false,
  dom_profile: {cells: [], n_snapshots: 0}, // session-wide resting profile
  positioning: null,                         // unified positioning snapshot
  positioning_timer: null,
  pivots: null,                              // {pivots:{P,R1..3,S1..3}, prior_day:{...}}
  vol_profile: null,                         // composite VPVR {cells, poc, vah, val, hvn, lvn, prior_pocs}
  vp_scope: 'prior_day',                     // 'prior_day' | 'week'
  resync_timer: null,                       // periodic running-bucket resync
};

// How often the running-bucket re-syncs against the authoritative server
// snapshot. Belt-and-suspenders against WS double-counting.
const RESYNC_MS = 5000;

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
    // Re-entering follow mode is a programmatic view change — force the
    // computed range to apply on the next redraw, overriding any manual
    // zoom/pan the user had done.
    if (state.autoscroll) { state.view_rev++; state.force_range = true; }
    state.dirty = true;
  });
  document.getElementById('reload').addEventListener('click', fullReload);

  // ── Volume-profile scope toggle (defensive: button may be absent if an
  //     old cached index.html is being served — never let that break boot) ─
  const vpBtn = document.getElementById('vp_scope');
  if (vpBtn) {
    vpBtn.addEventListener('click', async (e) => {
      state.vp_scope = state.vp_scope === 'prior_day' ? 'week' : 'prior_day';
      e.target.textContent = state.vp_scope === 'week' ? 'VP: week' : 'VP: prior day';
      await fetchVolProfile();
    });
  }

  // ── Zoom buttons ──────────────────────────────────────────────────────
  const stepZoom = (axis, dir) => {
    const cur = axis === 'x' ? state.zoom_x : state.zoom_y;
    const idx = ZOOM_STEPS.indexOf(cur);
    const ni  = Math.max(0, Math.min(ZOOM_STEPS.length - 1,
                  (idx < 0 ? ZOOM_STEPS.indexOf(1.0) : idx) + dir));
    const nv  = ZOOM_STEPS[ni];
    if (axis === 'x') state.zoom_x = nv; else state.zoom_y = nv;
    localStorage.setItem(`hawala_zoom_${axis}`, String(nv));
    updateZoomLabels();
    // Zoom buttons are programmatic view changes — force the computed range
    // to apply even if the user had manually dragged the chart.
    state.view_rev++; state.force_range = true; state.dirty = true;
  };
  document.getElementById('zoom_x_in' ).addEventListener('click', () => stepZoom('x',  1));
  document.getElementById('zoom_x_out').addEventListener('click', () => stepZoom('x', -1));
  document.getElementById('zoom_y_in' ).addEventListener('click', () => stepZoom('y',  1));
  document.getElementById('zoom_y_out').addEventListener('click', () => stepZoom('y', -1));
  document.getElementById('zoom_reset').addEventListener('click', () => {
    state.zoom_x = state.zoom_y = 1.0;
    localStorage.setItem('hawala_zoom_x', '1');
    localStorage.setItem('hawala_zoom_y', '1');
    // Reset also re-enters follow mode so the view snaps back to "live, full".
    state.autoscroll = true;
    const asBtn = document.getElementById('autoscroll');
    if (asBtn) asBtn.classList.add('btn-active');
    updateZoomLabels();
    state.view_rev++; state.force_range = true; state.dirty = true;
  });
  // Keyboard shortcuts: +/- = price zoom, [/] = time zoom, 0 = reset
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === '+' || e.key === '=') stepZoom('y',  1);
    else if (e.key === '-' || e.key === '_') stepZoom('y', -1);
    else if (e.key === ']') stepZoom('x',  1);
    else if (e.key === '[') stepZoom('x', -1);
    else if (e.key === '0') document.getElementById('zoom_reset').click();
  });
  updateZoomLabels();

  await fullReload();
  startRedrawLoop();
}

async function fetchVolProfile () {
  try {
    const q = new URLSearchParams({inst: state.inst, scope: state.vp_scope,
                                   cell: state.cell_size});
    const r = await fetch('/volume_profile?' + q).then(r => r.json());
    state.vol_profile = (r && !r.error) ? r : null;
  } catch (e) { state.vol_profile = null; }
  state.dirty = true;
}

function updateZoomLabels () {
  const xl = document.getElementById('zoom_x_lbl');
  const yl = document.getElementById('zoom_y_lbl');
  if (xl) xl.textContent = 'x' + state.zoom_x.toFixed(state.zoom_x < 1 ? 2 : 1);
  if (yl) yl.textContent = 'y' + state.zoom_y.toFixed(state.zoom_y < 1 ? 2 : 1);
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
  // Hard-close the old WS BEFORE we bump ws_seq so the close handler can't
  // race a reconnect onto the new generation.
  if (state.ws) {
    try { state.ws.onclose = null; state.ws.close(); } catch(e){}
    state.ws = null;
  }
  state.ws_seq += 1;                            // invalidate any in-flight msgs
  if (state.resync_timer) { clearInterval(state.resync_timer); state.resync_timer = null; }

  const q = new URLSearchParams({inst: state.inst, date: state.date,
                                 tf: state.tf, cell: state.cell_size});
  state.snap = await fetch('/snapshot?' + q).then(r => r.json());
  state.n_ticks = state.snap.n_ticks || 0;
  rebuildIndex();
  // Pivots: prior-day classic floor pivots. Stable for the whole session —
  // fetch once on reload.
  try {
    const pq = new URLSearchParams({inst: state.inst, date: state.date});
    const pr = await fetch('/pivots?' + pq).then(r => r.json());
    state.pivots = (pr && !pr.error) ? pr : null;
  } catch (e) { state.pivots = null; }
  // Composite volume profile (prior-day / weekly) — stable for the session,
  // fetch once on reload. Re-fetched when the user toggles scope.
  await fetchVolProfile();
  redrawAll();
  setStatus('live ✓');
  openWS();
  // Belt-and-suspenders: every RESYNC_MS, re-fetch /snapshot for the running
  // bucket only and overwrite local cells/candle for that bucket. Even if WS
  // double-counts, the in-progress candle never drifts more than 5s from truth.
  state.resync_timer = setInterval(resyncRunningBucket, RESYNC_MS);

  // Positioning sidebar — poll every 5 s on the same cadence
  if (state.positioning_timer) { clearInterval(state.positioning_timer); }
  fetchPositioning();
  state.positioning_timer = setInterval(fetchPositioning, RESYNC_MS);
}

async function fetchPositioning () {
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date});
    const r = await fetch('/positioning?' + q).then(r => r.json());
    if (r && !r.error) {
      state.positioning = r;
      renderPositioning(r);
    }
  } catch (e) { /* ignore — next tick retries */ }
}

function renderPositioning (p) {
  const apply = (id, c) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('bull', 'bear', 'flat');
    el.classList.add(c.dir > 0 ? 'bull' : c.dir < 0 ? 'bear' : 'flat');
    el.querySelector('.pos-value').textContent =
      (c.value > 0 ? '+' : '') + c.value.toFixed(2);
    el.querySelector('.pos-sub').textContent = c.label || '—';
  };
  apply('pos_flow',         p.flow);
  apply('pos_resting',      p.resting);
  apply('pos_institutions', p.institutions);
  apply('pos_macro',        p.macro);
  apply('pos_composite',    p.composite);

  // When all four components point the same non-flat direction, highlight
  // the composite card with a gold halo — that's the "all aligned" moment.
  const comp = document.getElementById('pos_composite');
  comp.classList.toggle('aligned', !!p.composite.aligned);

  // Extra context on individual cards. Each gets a freshness suffix so the
  // user can tell at a glance which inputs are stale vs live (the panel
  // refreshes every 5 s, but the underlying inputs update on different
  // cadences — flow is a 5-min rolling avg, INST writes once/min, MACRO
  // every several minutes).
  const now = Date.now();
  const ageStr = (updated_ms) => {
    if (!updated_ms) return '—';
    const sec = Math.max(0, Math.round((now - updated_ms) / 1000));
    if (sec < 60)  return `${sec}s ago`;
    if (sec < 3600) return `${Math.round(sec/60)}m ago`;
    return `${(sec/3600).toFixed(1)}h ago`;
  };
  document.querySelector('#pos_flow .pos-sub').textContent =
    `${p.flow.label} · ${p.flow.n_ticks}t · ${p.flow.window_min || 5}min win · ${ageStr(p.flow.updated_ms)}`;
  document.querySelector('#pos_resting .pos-sub').textContent =
    `${p.resting.label} · b${fmtNum(p.resting.bid_qty)}/a${fmtNum(p.resting.ask_qty)} · ${ageStr(p.resting.updated_ms)}`;
  document.querySelector('#pos_institutions .pos-sub').textContent =
    `${p.institutions.label} · conv ${p.institutions.conv.toFixed(2)} · ${ageStr(p.institutions.updated_ms)}`;
  document.querySelector('#pos_macro .pos-sub').textContent =
    `${p.macro.label} · conf ${(p.macro.confidence*100).toFixed(0)}% · ${ageStr(p.macro.updated_ms)}`;

  document.getElementById('pos_updated').textContent =
    `polled ${new Date().toLocaleTimeString()}`;
}

async function resyncRunningBucket () {
  if (state.paused) return;
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date,
                                   tf: state.tf, cell: state.cell_size});
    const fresh = await fetch('/snapshot?' + q).then(r => r.json());
    if (!fresh || !fresh.candles || fresh.candles.length === 0) return;
    // Identify the latest (running) bucket from server truth.
    const latestBk = Math.max(...fresh.candles.map(c => c.bucket));
    // Replace cells for this bucket with server values (drop existing).
    state.snap.cells = state.snap.cells.filter(c => c.bucket !== latestBk);
    for (const c of fresh.cells) {
      if (c.bucket === latestBk) {
        state.snap.cells.push(c);
        state.cells_idx.set(`${c.bucket}|${c.cell}`, c);
      }
    }
    // Replace candle for this bucket
    const fc = fresh.candles.find(k => k.bucket === latestBk);
    if (fc) state.candles_idx.set(latestBk, fc);
    // Drop any stale cell-index entries for this bucket that no longer exist
    for (const key of [...state.cells_idx.keys()]) {
      const [bk] = key.split('|');
      if (Number(bk) === latestBk &&
          !state.snap.cells.find(c => `${c.bucket}|${c.cell}` === key)) {
        state.cells_idx.delete(key);
      }
    }
    state.dirty = true;
    // Refresh the session DOM profile on the same 5s cadence — the running
    // candle and the resting-order picture stay in lockstep with truth.
    fetchDomProfile();
  } catch (e) { /* network blips are fine, next tick will retry */ }
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
  // Capture the generation this socket belongs to. Any message arriving
  // after a fullReload (which bumps ws_seq) is silently ignored.
  const mySeq = state.ws_seq;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws_url = `${proto}://${location.host}/ws?inst=${state.inst}&date=${state.date}`;
  const ws = new WebSocket(state.ws_url);
  state.ws = ws;
  document.getElementById('ws_state').textContent = 'ws: connecting';
  ws.addEventListener('open',  () => {
    if (mySeq !== state.ws_seq) return;
    document.getElementById('ws_state').textContent = 'ws: ✓ open';
  });
  ws.addEventListener('close', () => {
    if (mySeq !== state.ws_seq) return;        // stale — caller already reset
    document.getElementById('ws_state').textContent = 'ws: closed (retrying)';
    if (!state.paused) setTimeout(() => {
      if (mySeq === state.ws_seq) openWS();
    }, 1500);
  });
  ws.addEventListener('error', () => {
    if (mySeq !== state.ws_seq) return;
    document.getElementById('ws_state').textContent = 'ws: err';
  });
  ws.addEventListener('message', ev => {
    if (mySeq !== state.ws_seq) return;        // stale socket — drop msg
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
    // Feed-stall messages from the server-side WS streamer
    // (viewer/live_server.py emits these when no fresh ticks for ≥90 s
    // during market hours — the recorder is probably reconnecting).
    if (m.type === 'stall') showStallBadge(m.stall_age_s);
    if (m.type === 'resume') hideStallBadge();
  });

  // Initial depth fetch — server pushes only on new ts; seed via REST.
  fetch(`/depth?inst=${state.inst}&date=${state.date}`)
    .then(r => r.json())
    .then(d => { if (mySeq === state.ws_seq) { state.depth = d; state.depth_dirty = true; } })
    .catch(() => {});

  // Session-wide DOM profile — fetched once at WS open, refreshed by the
  // 5-second resync loop alongside the running candle.
  fetchDomProfile(mySeq);
}

async function fetchDomProfile (mySeq) {
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date,
                                   cell: state.cell_size});
    const r = await fetch('/dom_profile?' + q).then(r => r.json());
    if (mySeq !== undefined && mySeq !== state.ws_seq) return;  // stale
    state.dom_profile = r;
    state.dirty = true;
  } catch (e) { /* ignore */ }
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

  // ── Absorption + reversal badges ───────────────────────────────────────
  // Absorption: large |delta| but the candle barely moved (passive orders
  //   soaked up the aggression). High conviction one-sided flow that FAILED
  //   to move price = the level is being defended. Marker: 🅐 above the bar.
  // Reversal: delta sign flips vs the prior bar AT a local extreme (the high
  //   of an up-run or low of a down-run). Marker: ⤣ arrow at the turn.
  // Both computed from data already in the snapshot — no new capture.
  const sortedC = candles;   // already bucket-sorted
  const bodies  = sortedC.map(c => Math.abs(c.close - c.open));
  const medBody = bodies.slice().sort((a,b)=>a-b)[Math.floor(bodies.length/2)] || cs;
  const deltas  = sortedC.map(c => c.delta_qty || 0);
  const absDeltas = deltas.map(Math.abs).sort((a,b)=>a-b);
  const p70Delta  = absDeltas[Math.floor(absDeltas.length*0.70)] || 0;

  for (let i = 0; i < sortedC.length; i++) {
    const c = sortedC[i];
    const body = Math.abs(c.close - c.open);
    const d    = c.delta_qty || 0;
    // Absorption: delta in top-30% of the session AND body ≤ 60% of median.
    if (Math.abs(d) >= p70Delta && p70Delta > 0 && body <= 0.6 * medBody) {
      annos.push({
        x: c.bucket, y: c.high + 1.2*cs, xref:'x', yref:'y',
        text:'🅐', showarrow:false, font:{size:12},
        hovertext:`Absorption — Δ=${fmtNum(d)} but body only ${body.toFixed(1)}pts. `
                + `Aggression absorbed; level defended.`,
      });
    }
    // Reversal: delta flips sign vs prior bar, at a local extreme.
    if (i > 0) {
      const prev = deltas[i-1];
      const flip = (prev > 0 && d < 0) || (prev < 0 && d > 0);
      const isHi = i>0 && i<sortedC.length-1 && c.high >= sortedC[i-1].high && c.high >= sortedC[i+1]?.high;
      const isLo = i>0 && i<sortedC.length-1 && c.low  <= sortedC[i-1].low  && c.low  <= sortedC[i+1]?.low;
      if (flip && (isHi || isLo)) {
        annos.push({
          x: c.bucket, y: isHi ? c.high + 1.2*cs : c.low - 1.2*cs,
          xref:'x', yref:'y', text: isHi ? '⤵' : '⤴',
          showarrow:false, font:{size:13, color: isHi ? '#ef5350' : '#26a69a'},
          hovertext:`Delta-flip reversal at local ${isHi?'high':'low'} `
                  + `(Δ ${fmtNum(prev)}→${fmtNum(d)})`,
        });
      }
    }
  }

  // ── Visible window (data-driven, no synthetic padding) ─────────────────
  // x: at zoom 1.0 show every actual candle (no pre-09:15 blanks). Zoom > 1
  // narrows to the most recent slice. Clamped so we always see ≥6 candles.
  const lastBk  = candles[candles.length-1].bucket;
  const firstBk = candles[0].bucket;
  const visCandles = Math.max(6, Math.round(candles.length / state.zoom_x));
  // Always derive the window from zoom_x (don't gate on autoscroll). If we
  // gated it, the ⇤⇥ toolbar zoom buttons would silently do nothing once the
  // user had manually interacted (which auto-disables autoscroll). At
  // zoom_x=1 visCandles == candles.length so startBk == firstBk (full range).
  // The window is only *applied* to the axis when applyRange is true (follow
  // mode or a button's force_range); native drag/pan is preserved otherwise.
  const startBk = Math.max(firstBk, lastBk - (visCandles - 1) * tf_ms);
  const xRange = [startBk - tf_ms * 0.5, lastBk + tf_ms * 0.5];

  // y: auto-fit to visible candles' highs/lows. Zoom_y contracts that span
  // around the latest close → broker-style price-axis zoom.
  const visibleCandles = candles.filter(c => c.bucket >= startBk);
  const yLo0 = Math.min(...visibleCandles.map(c => c.low));
  const yHi0 = Math.max(...visibleCandles.map(c => c.high));
  const yMid = candles[candles.length-1].close;
  const yHalfSpan = ((yHi0 - yLo0) / 2 + 2 * cs) / state.zoom_y;
  const yRange = [yMid - yHalfSpan, yMid + yHalfSpan];

  // ── Pivot lines (classic floor pivots from prior day's OHLC) ──────────
  // Drawn AFTER xRange/yRange are defined. Levels outside the visible
  // y-window are skipped so labels don't hang in space.
  if (state.pivots && state.pivots.pivots) {
    const piv = state.pivots.pivots;
    const levels = [
      {k:'R3', v:piv.R3, color:'#b71c1c', dash:'dot',    weight:0.9},
      {k:'R2', v:piv.R2, color:'#ef5350', dash:'dash',   weight:1.0},
      {k:'R1', v:piv.R1, color:'#ef5350', dash:'solid',  weight:1.2},
      {k:'P',  v:piv.P,  color:'#fdd835', dash:'dash',   weight:1.4},
      {k:'S1', v:piv.S1, color:'#26a69a', dash:'solid',  weight:1.2},
      {k:'S2', v:piv.S2, color:'#26a69a', dash:'dash',   weight:1.0},
      {k:'S3', v:piv.S3, color:'#0d6e63', dash:'dot',    weight:0.9},
    ];
    for (const L of levels) {
      if (L.v < yRange[0] - cs || L.v > yRange[1] + cs) continue;
      shapes.push({
        type:'line', xref:'x', yref:'y',
        x0: xRange[0], x1: xRange[1], y0: L.v, y1: L.v,
        line: {color: L.color, width: L.weight, dash: L.dash},
        layer: 'below',
      });
      annos.push({
        x: xRange[0], y: L.v, xref:'x', yref:'y',
        text: `<b>${L.k}</b> ${L.v.toFixed(1)}`,
        showarrow:false, xanchor:'left', yanchor:'middle',
        bgcolor:'rgba(255,255,255,0.85)', bordercolor:L.color, borderwidth:1,
        font:{size:9, color:L.color}, borderpad:2,
      });
    }
  }

  // ── Composite Volume Profile overlay (prior-day / weekly) ──────────────
  // Drawn as: translucent value-area band, bold POC line, naked-POC magnet
  // lines, and HVN/LVN markers in the left margin. These are the *completed
  // prior session(s)* levels — actionable from the open, unlike today's
  // still-forming profile.
  if (state.vol_profile && state.vol_profile.cells && state.vol_profile.cells.length) {
    const vp = state.vol_profile;
    const scopeLbl = vp.scope === 'week' ? 'wk' : 'pd';
    const inView = (y) => y >= yRange[0] - cs && y <= yRange[1] + cs;

    // Value-area band (translucent purple) between VAL and VAH
    if (vp.val != null && vp.vah != null) {
      shapes.push({
        type:'rect', xref:'x', yref:'y', layer:'below',
        x0: xRange[0], x1: xRange[1], y0: vp.val, y1: vp.vah,
        fillcolor:'rgba(123,31,162,0.07)', line:{width:0},
      });
    }
    // POC — bold purple line
    if (vp.poc != null && inView(vp.poc)) {
      shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
        x0:xRange[0], x1:xRange[1], y0:vp.poc, y1:vp.poc,
        line:{color:'#7b1fa2', width:1.8}});
      annos.push({x:xRange[0], y:vp.poc, xref:'x', yref:'y',
        text:`<b>POC·${scopeLbl}</b> ${vp.poc.toFixed(0)}`,
        showarrow:false, xanchor:'left', yanchor:'bottom',
        bgcolor:'rgba(255,255,255,0.85)', bordercolor:'#7b1fa2', borderwidth:1,
        font:{size:9, color:'#7b1fa2'}, borderpad:2});
    }
    // VAH / VAL dashed bounds
    [['VAH', vp.vah], ['VAL', vp.val]].forEach(([lbl, y]) => {
      if (y != null && inView(y)) {
        shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
          x0:xRange[0], x1:xRange[1], y0:y, y1:y,
          line:{color:'rgba(123,31,162,0.5)', width:0.8, dash:'dot'}});
      }
    });
    // Naked POCs — prior-day POCs today's price hasn't traded back through.
    // A POC is "naked" if it's OUTSIDE the current visible candle range
    // extremes seen so far today (proxy: not between today's hi/lo).
    const todayHi = Math.max(...candles.map(c => c.high));
    const todayLo = Math.min(...candles.map(c => c.low));
    (vp.prior_pocs || []).forEach(pp => {
      const naked = pp.poc > todayHi || pp.poc < todayLo;
      if (naked && inView(pp.poc)) {
        shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
          x0:xRange[0], x1:xRange[1], y0:pp.poc, y1:pp.poc,
          line:{color:'#ff6f00', width:1.2, dash:'dash'}});
        annos.push({x:xRange[1], y:pp.poc, xref:'x', yref:'y',
          text:`nPOC ${pp.date.slice(5)}`,
          showarrow:false, xanchor:'right', yanchor:'bottom',
          bgcolor:'rgba(255,255,255,0.8)', bordercolor:'#ff6f00', borderwidth:1,
          font:{size:8, color:'#ff6f00'}, borderpad:1});
      }
    });
    // HVN markers (acceptance walls) — left-margin purple ▸ (now capped to
    // top 6 server-side, so no clutter).
    (vp.hvn || []).forEach(y => {
      if (inView(y)) annos.push({
        x: xRange[0], y, xref:'x', yref:'y', xanchor:'left', yanchor:'middle',
        text:'▸', showarrow:false, font:{size:11, color:'#7b1fa2'},
        hovertext:`HVN ${y.toFixed(0)} — acceptance wall (hard to break)`,
      });
    });
    // LVN gap ZONES — shaded grey rectangles (rejection vacuums where price
    // slices through fast). Clustered server-side into ≤3 zones, not spam.
    (vp.lvn_zones || []).forEach(z => {
      if (z.hi < yRange[0] || z.lo > yRange[1]) return;
      shapes.push({
        type:'rect', xref:'x', yref:'y', layer:'below',
        x0:xRange[0], x1:xRange[1], y0:z.lo, y1:z.hi,
        fillcolor:'rgba(120,120,120,0.10)',
        line:{width:0.5, color:'rgba(120,120,120,0.4)', dash:'dot'},
      });
      annos.push({
        x: xRange[0], y: (z.lo + z.hi) / 2, xref:'x', yref:'y',
        xanchor:'left', yanchor:'middle', text:'▹ gap',
        showarrow:false, font:{size:8, color:'#999'},
        hovertext:`LVN vacuum ${z.lo.toFixed(0)}-${z.hi.toFixed(0)} — `
                + `thin volume, price slices through fast (no support here)`,
      });
    });
  }

  // ── High-probability entry markers (scored, multi-factor) ─────────────
  // The old version fired on ANY candle touching ANY of ~8 levels — pure
  // noise. This requires a CONFLUENCE of evidence and only marks setups
  // scoring ≥ ENTRY_MIN. Factors (each adds points):
  //   level quality   value-area EDGE (VAL/VAH) or naked POC = 2;
  //                   pivot-confluence (VP level ≈ floor pivot) = +2;
  //                   POC = 1; (interior HVN alone never triggers)
  //   rejection wick  rejecting wick ≥ 55% of the bar's range = 2
  //   order flow      reversal-direction delta ≥ session 70th pctile = 2
  //   absorption      high delta + tiny body at the level = +1
  //   DOM wall        persistent resting wall (bid for longs / ask for
  //                   shorts) within 1 cell of the level = +1
  // A genuine bounce usually scores 6-8; random touches score 1-3.
  // Threshold 6 requires ~3 strong factors stacking (e.g. value-edge +
  // confluence + rejection wick, or value-edge + wick + significant delta)
  // — deliberately rare. Lower to 5 if you want more candidates.
  const ENTRY_MIN = 6;
  if (state.vol_profile && candles.length >= 3) {
    const vp2  = state.vol_profile;
    const piv  = (state.pivots && state.pivots.pivots) || {};
    const lastClose = candles[candles.length - 1].close;
    const tol  = cs * 1.0;
    const confTol = cs * 1.5;   // VP level ≈ pivot → confluence

    // Build support + resistance level sets with a base quality score.
    const pivVals = ['R1','R2','R3','P','S1','S2','S3']
      .map(k => piv[k]).filter(v => v != null);
    const isConfluent = (px) => pivVals.some(pv => Math.abs(pv - px) <= confTol);
    const sup = [], res = [];
    if (vp2.val != null) sup.push({px:vp2.val, name:'VAL', base:2});
    if (vp2.vah != null) res.push({px:vp2.vah, name:'VAH', base:2});
    if (vp2.poc != null) { sup.push({px:vp2.poc,name:'POC',base:1});
                           res.push({px:vp2.poc,name:'POC',base:1}); }
    (vp2.prior_pocs || []).forEach(pp => {
      (pp.poc < lastClose ? sup : res).push(
        {px: pp.poc, name:`nPOC·${pp.date.slice(5)}`, base:2});
    });

    // DOM persistence lookup — is there a resting wall near a price?
    const domCells = (state.dom_profile && state.dom_profile.cells) || [];
    const domWall = (px, side) => domCells.some(dc =>
      Math.abs(dc.cell - px) <= tol &&
      (side === 'bid' ? dc.bid_persistence : dc.ask_persistence) >= 0.30);

    for (let i = 0; i < candles.length; i++) {
      const c = candles[i];
      const rng = Math.max(c.high - c.low, 1e-6);
      const body = Math.abs(c.close - c.open);
      const d = c.delta_qty || 0;
      const lowerWick = Math.min(c.open, c.close) - c.low;
      const upperWick = c.high - Math.max(c.open, c.close);
      const absorb = Math.abs(d) >= p70Delta && p70Delta > 0 && body <= 0.6*medBody;

      // ── LONG: rejection of a support level ──
      let bestL = null, bestScore = 0;
      for (const L of sup) {
        if (c.low > L.px + tol || c.close <= L.px) continue;  // must wick+reclaim
        let s = L.base;
        if (isConfluent(L.px))               s += 2;
        if (lowerWick / rng >= 0.55)          s += 2;
        if (d > 0 && d >= p70Delta)           s += 2;
        if (absorb && d > 0)                  s += 1;
        if (domWall(L.px, 'bid'))             s += 1;
        if (s > bestScore) { bestScore = s; bestL = L; }
      }
      if (bestL && bestScore >= ENTRY_MIN) {
        annos.push({x:c.bucket, y:c.low - 1.5*cs, xref:'x', yref:'y',
          text:'▲', showarrow:false, font:{size:13, color:'#1b5e20'},
          hovertext:`LONG candidate · score ${bestScore} · rejected ${bestL.name} `
                  + `${bestL.px.toFixed(0)}${isConfluent(bestL.px)?' (pivot confluence)':''} · `
                  + `+delta ${fmtNum(d)}${absorb?' · absorption':''}`
                  + `${domWall(bestL.px,'bid')?' · bid-wall':''}`});
        continue;   // one marker per candle
      }

      // ── SHORT: rejection of a resistance level ──
      bestL = null; bestScore = 0;
      for (const L of res) {
        if (c.high < L.px - tol || c.close >= L.px) continue;
        let s = L.base;
        if (isConfluent(L.px))               s += 2;
        if (upperWick / rng >= 0.55)          s += 2;
        if (d < 0 && Math.abs(d) >= p70Delta) s += 2;
        if (absorb && d < 0)                  s += 1;
        if (domWall(L.px, 'ask'))             s += 1;
        if (s > bestScore) { bestScore = s; bestL = L; }
      }
      if (bestL && bestScore >= ENTRY_MIN) {
        annos.push({x:c.bucket, y:c.high + 1.5*cs, xref:'x', yref:'y',
          text:'▼', showarrow:false, font:{size:13, color:'#b71c1c'},
          hovertext:`SHORT candidate · score ${bestScore} · rejected ${bestL.name} `
                  + `${bestL.px.toFixed(0)}${isConfluent(bestL.px)?' (pivot confluence)':''} · `
                  + `-delta ${fmtNum(d)}${absorb?' · absorption':''}`
                  + `${domWall(bestL.px,'ask')?' · ask-wall':''}`});
      }
    }
  }

  // y-tick density: aim for ~10 labels regardless of cell size / zoom.
  // Round step to the nearest "nice" multiple of cell size.
  const ySpan = yRange[1] - yRange[0];
  const rawStep = ySpan / 10;
  const niceMults = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500];
  let yDtick = cs;
  for (const m of niceMults) {
    if (m * cs >= rawStep) { yDtick = m * cs; break; }
  }

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

  // ── Session DOM profile — mean resting qty per price level over the day.
  // Plotted as faint background bars BEHIND the live top-5 in the same pane:
  // bars further from zero = bigger walls; persistent levels (high % of
  // snapshots) get higher opacity. Outline highlight on the very top
  // walls so the eye lands on real liquidity nodes.
  const profCells = (state.dom_profile && state.dom_profile.cells) || [];
  // Find the largest walls so we can highlight the top-N
  const allWalls = profCells.flatMap(c => [
    {side:'bid', px:c.cell, qty:c.mean_bid_qty, persist:c.bid_persistence},
    {side:'ask', px:c.cell, qty:c.mean_ask_qty, persist:c.ask_persistence},
  ]).filter(w => w.qty > 0);
  const wallMax = Math.max(1, ...allWalls.map(w => w.qty));
  // Top-3 walls each side: ones we annotate with a label + outline
  const topBidWalls = [...allWalls].filter(w => w.side === 'bid')
                       .sort((a,b) => b.qty - a.qty).slice(0, 3);
  const topAskWalls = [...allWalls].filter(w => w.side === 'ask')
                       .sort((a,b) => b.qty - a.qty).slice(0, 3);
  const isTopBid = new Set(topBidWalls.map(w => w.px));
  const isTopAsk = new Set(topAskWalls.map(w => w.px));

  const sessBidPx = profCells.map(c => c.cell);
  const sessBidQ  = profCells.map(c => c.mean_bid_qty);
  const sessAskPx = profCells.map(c => c.cell);
  const sessAskQ  = profCells.map(c => -c.mean_ask_qty);
  // Opacity ∝ persistence: a level present in 50%+ of snapshots looks
  // solid; an ephemeral one fades back. Plus a tiny floor so 1-2% levels
  // still draw something.
  const sessBidColors = profCells.map(c =>
    `rgba(38,166,154,${(0.10 + 0.55 * Math.min(1, c.bid_persistence * 1.5)).toFixed(2)})`);
  const sessAskColors = profCells.map(c =>
    `rgba(239,83,80,${(0.10 + 0.55 * Math.min(1, c.ask_persistence * 1.5)).toFixed(2)})`);
  // Outline highlight on top walls
  const sessBidLine = profCells.map(c =>
    isTopBid.has(c.cell) ? {color:'#0d6e63', width:1.5} : {width:0});
  const sessAskLine = profCells.map(c =>
    isTopAsk.has(c.cell) ? {color:'#b71c1c', width:1.5} : {width:0});
  // Labels: only on the top-3 walls each side to keep the pane readable
  const sessBidLabels = profCells.map(c =>
    isTopBid.has(c.cell) ? fmtNum(c.mean_bid_qty) : '');
  const sessAskLabels = profCells.map(c =>
    isTopAsk.has(c.cell) ? fmtNum(c.mean_ask_qty) : '');

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

  // Stride x-tick labels so a dense tf stays readable. When zoomed in to ≤20
  // candles we label every one.
  let xLabelStride = X_TICK_LABEL_EVERY[state.tf] || 1;
  if (visCandles <= 20) xLabelStride = 1;

  // ── Range + uirevision: make manual zoom/pan STICK ────────────────────
  // Plotly preserves user zoom/pan across redraws iff `uirevision` is
  // unchanged. We only force our computed range when in follow mode OR a
  // button just demanded it (force_range one-shot). Otherwise the user's
  // drag-zoom ("snip") and horizontal scroll persist through the 200ms /
  // 5s redraws instead of being wiped.
  const applyRange = state.autoscroll || state.force_range;
  state.force_range = false;
  const viewKey = `${state.inst}|${state.tf}|${state.date}|${state.cell_size}`;
  // In follow mode, advancing lastBk bumps the revision so the range tracks
  // new candles. In manual mode, only a button (view_rev) changes it, so
  // native interaction sticks between button presses.
  const uirev = state.autoscroll
    ? `${viewKey}|follow|${state.view_rev}|${lastBk}`
    : `${viewKey}|manual|${state.view_rev}`;
  const xRangeOpt = applyRange ? {range: xRange} : {};
  const yRangeOpt = applyRange ? {range: yRange} : {};

  const layout = {
    paper_bgcolor:'#fafafa', plot_bgcolor:'#fff',
    margin:{l:55, r:15, t:25, b:42},
    showlegend:false,
    // domains: main=72%, session profile=12%, live DOM=14% (right edge).
    xaxis:  {type:'date', domain:[0, 0.72], ...xRangeOpt,
             dtick: tf_ms * xLabelStride, tickformat:'%H:%M', tick0: lastBk,
             gridcolor:'#eef0f2', gridwidth:1, showspikes:false},
    yaxis:  {domain:[0.27, 1], title:'price', side:'left', ...yRangeOpt,
             dtick: yDtick, tickformat:',d',
             gridcolor:'#eef0f2', gridwidth:1},
    // Session BS-summary pane — show qty labels at bottom so user knows scale
    xaxis2: {domain:[0.73, 0.84], anchor:'y2', title:'BS qty',
             showticklabels:true, tickfont:{size:9}, nticks:3,
             gridcolor:'#eef0f2'},
    yaxis2: {domain:[0.27, 1], matches:'y', showticklabels:false},
    // Delta pane — matches:'x' so it ALWAYS mirrors the candle pane's
    // zoom/pan (timestamps stay aligned, and panning the candles scrolls
    // the delta histogram in lockstep — no separate range to fight).
    xaxis3: {type:'date', domain:[0, 0.72], anchor:'y3', matches:'x',
             tickformat:'%H:%M', gridcolor:'#eef0f2',
             showticklabels:true, tickfont:{size:9}},
    yaxis3: {domain:[0, 0.22], title:'Δ qty', gridcolor:'#eef0f2',
             tickfont:{size:9}, nticks:4},
    // Live DOM + session profile pane — show qty labels
    xaxis4: {domain:[0.86, 1.0], anchor:'y4', title:'DOM (live + session)',
             showticklabels:true, tickfont:{size:9}, nticks:3,
             zeroline:true, zerolinecolor:'#999', zerolinewidth:1,
             gridcolor:'#eef0f2'},
    yaxis4: {domain:[0.27, 1], matches:'y', showticklabels:false},
    shapes, annotations: annos,
    barmode:'overlay',
    uirevision: uirev,
  };

  const traces = [
    // anchor for main pane axis type
    {x:[candles[0].bucket, candles[candles.length-1].bucket],
     y:[Math.min(...candles.map(c=>c.low))  - 1.5*cs,
        Math.max(...candles.map(c=>c.high)) + 1.5*cs],
     mode:'markers', marker:{size:0.1, color:'rgba(0,0,0,0)'},
     hoverinfo:'skip', xaxis:'x', yaxis:'y'},
    // session profile (pane #2) — sell side (left of axis, negative x)
    {x: profSell, y: profY, type:'bar', orientation:'h',
     xaxis:'x2', yaxis:'y2',
     marker:{color:'rgba(239,83,80,0.85)'},
     text: prof.map(p => fmtNum(profUseQty ? p.sell_qty : p.sell_ticks)),
     textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#b71c1c'},
     hovertemplate:'price=%{y}<br>sell=%{x:.0f}<extra></extra>'},
    // session profile (pane #2) — buy side (right of axis, positive x)
    {x: profBuy,  y: profY, type:'bar', orientation:'h',
     xaxis:'x2', yaxis:'y2',
     marker:{color:'rgba(38,166,154,0.85)'},
     text: prof.map(p => fmtNum(profUseQty ? p.buy_qty : p.buy_ticks)),
     textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#0d6e63'},
     hovertemplate:'price=%{y}<br>buy=%{x:.0f}<extra></extra>'},
    // delta histogram (bottom pane) — numeric labels on top of positive bars,
    // bottom of negative bars (Plotly's 'outside' handles this automatically).
    {x: dx, y: dy, type:'bar', marker:{color: dcol},
     xaxis:'x3', yaxis:'y3',
     text: dy.map(v => fmtNum(Math.abs(v))),
     textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#333'},
     hovertemplate:'%{x}<br>Δ=%{y:.0f}<extra></extra>'},
    // ─ Session DOM profile (drawn FIRST so live top-5 lands on top) ──────
    // Faint full-day mean resting qty bars. Top-3 walls each side get
    // outlines + qty labels so genuine liquidity nodes are obvious.
    {x: sessBidQ, y: sessBidPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4', width: cs * 0.9,
     marker:{color: sessBidColors, line: {color:'rgba(0,0,0,0)', width:0}},
     hovertemplate:'session BID %{y}<br>mean qty=%{x:.0f}<extra></extra>',
     showlegend:false},
    {x: sessAskQ, y: sessAskPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4', width: cs * 0.9,
     marker:{color: sessAskColors, line: {color:'rgba(0,0,0,0)', width:0}},
     hovertemplate:'session ASK %{y}<br>mean qty=%{x:.0f}<extra></extra>',
     showlegend:false},
    // Outline+label overlay for the top-3 walls each side (drawn as a
    // separate trace because Plotly bar `marker.line` doesn't accept arrays
    // for orientation:'h' consistently across versions).
    {x: profCells.map(c => isTopBid.has(c.cell) ? c.mean_bid_qty : 0),
     y: profCells.map(c => c.cell),
     type:'bar', orientation:'h', xaxis:'x4', yaxis:'y4', width: cs * 0.9,
     marker:{color:'rgba(0,0,0,0)', line:{color:'#0d6e63', width:1.5}},
     text: sessBidLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#0d6e63'},
     hoverinfo:'skip', showlegend:false},
    {x: profCells.map(c => isTopAsk.has(c.cell) ? -c.mean_ask_qty : 0),
     y: profCells.map(c => c.cell),
     type:'bar', orientation:'h', xaxis:'x4', yaxis:'y4', width: cs * 0.9,
     marker:{color:'rgba(0,0,0,0)', line:{color:'#b71c1c', width:1.5}},
     text: sessAskLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#b71c1c'},
     hoverinfo:'skip', showlegend:false},
    // ─ Live top-5 DOM (foreground, more saturated) ───────────────────────
    {x: domBidQ, y: domBidPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4', width: cs * 0.4,
     marker:{color: bidColors,
             line:{color:'#0d6e63', width:1.0}},
     text: bidLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:10, color:'#0d6e63'},
     hovertemplate:'LIVE BID %{y}<br>qty=%{x}<extra></extra>'},
    {x: domAskQ, y: domAskPx, type:'bar', orientation:'h',
     xaxis:'x4', yaxis:'y4', width: cs * 0.4,
     marker:{color: askColors,
             line:{color:'#b71c1c', width:1.0}},
     text: askLabels, textposition:'outside', cliponaxis:false,
     textfont:{size:10, color:'#b71c1c'},
     hovertemplate:'LIVE ASK %{y}<br>qty=%{x:.0f}<extra></extra>'},
  ];

  Plotly.react('chart', traces, layout,
               {responsive:true, displaylogo:false, scrollZoom:true});

  // Wire the user-interaction listener ONCE (persists across Plotly.react).
  // When the user box-zooms ("snip") or pans the x-axis, drop out of follow
  // mode so their view sticks instead of being re-applied on the next redraw.
  if (!state._relayout_wired) {
    const chartDiv = document.getElementById('chart');
    if (chartDiv && chartDiv.on) {
      chartDiv.on('plotly_relayout', (ev) => {
        const touched = Object.keys(ev || {}).some(k =>
          k.startsWith('xaxis.range') || k.startsWith('yaxis.range'));
        if (touched && state.autoscroll) {
          state.autoscroll = false;
          const b = document.getElementById('autoscroll');
          if (b) b.classList.remove('btn-active');
        }
      });
      state._relayout_wired = true;
    }
  }

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

// ─── Feed-stall badge ───────────────────────────────────────────────────────
// Server sends {type:'stall', stall_age_s} when no ticks for ≥90 s during
// market hours, and {type:'resume'} when ticks come back. We show a small
// pulsing chip in the topbar so the user knows the recorder is reconnecting
// (vs the market genuinely being quiet). The badge is created lazily on
// first stall to keep the HTML clean.
function _ensureStallBadge () {
  let el = document.getElementById('stall_badge');
  if (el) return el;
  el = document.createElement('span');
  el.id = 'stall_badge';
  el.className = 'stall-badge';
  el.title = 'No fresh ticks reaching the viewer — recorder likely reconnecting. '
           + 'The process monitor will restart it if it doesn’t recover.';
  document.querySelector('.controls')?.appendChild(el);
  return el;
}

function showStallBadge (stallAgeSec) {
  const el = _ensureStallBadge();
  el.textContent = `⚠ feed stale ${Math.round(stallAgeSec)}s`;
  el.style.display = 'inline-block';
}

function hideStallBadge () {
  const el = document.getElementById('stall_badge');
  if (el) el.style.display = 'none';
}

document.addEventListener('DOMContentLoaded', bootstrap);
