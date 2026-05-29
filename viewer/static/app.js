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
  // Clean mode (default) = candles + key levels (POC/VAH/VAL + pivots) +
  // positioning only. Analysis mode reveals the experimental, UNVALIDATED
  // overlays (entry markers, absorption/reversal badges, HVN/LVN, naked POCs)
  // — for study, never for trading. Persisted so it stays Clean across reloads.
  analysis_mode: localStorage.getItem('hawala_analysis_mode') === '1',
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
  option_levels: null,                       // option-OI S&R {ce_resistance, pe_support, max_pain, ...}
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
  state.today = cfg.today;   // server "today" — used to detect replay vs live

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

  // Positioning panel: click the title to collapse/expand (it can otherwise
  // overlap the right-hand DOM/profile panes). State persists across reloads.
  const posTitle = document.querySelector('.pos-title');
  const posPanel = document.getElementById('positioning');
  if (posTitle && posPanel) {
    posTitle.addEventListener('click', () => {
      posPanel.classList.toggle('collapsed');
      localStorage.setItem('hawala_pos_collapsed',
        posPanel.classList.contains('collapsed') ? '1' : '0');
    });
    if (localStorage.getItem('hawala_pos_collapsed') === '1')
      posPanel.classList.add('collapsed');
  }

  // Full-screen toggle (whole document → immersive chart).
  const fsBtn = document.getElementById('fullscreen');
  if (fsBtn) fsBtn.addEventListener('click', () => {
    if (!document.fullscreenElement) {
      (document.documentElement.requestFullscreen || (()=>{})).call(document.documentElement);
    } else {
      (document.exitFullscreen || (()=>{})).call(document);
    }
  });

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

  // ── Clean / Analysis mode toggle ──────────────────────────────────────
  const vmBtn = document.getElementById('view_mode');
  if (vmBtn) {
    const paint = () => {
      vmBtn.textContent = state.analysis_mode ? '🔬 Analysis' : '🧭 Clean';
      vmBtn.classList.toggle('btn-active', state.analysis_mode);
    };
    paint();
    vmBtn.addEventListener('click', () => {
      state.analysis_mode = !state.analysis_mode;
      localStorage.setItem('hawala_analysis_mode', state.analysis_mode ? '1' : '0');
      paint();
      state.dirty = true;   // redraw to add/remove experimental overlays
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
                                   cell: state.cell_size, date: state.date});
    const r = await fetch('/volume_profile?' + q).then(r => r.json());
    state.vol_profile = (r && !r.error) ? r : null;
  } catch (e) { state.vol_profile = null; }
  state.dirty = true;
}

async function fetchOptionLevels () {
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date});
    const r = await fetch('/option_levels?' + q).then(r => r.json());
    state.option_levels = (r && r.available) ? r : null;
  } catch (e) { state.option_levels = null; }
  state.dirty = true;
}

// Live contango (futures − spot) for the header chip + heartbeat price tag.
async function fetchBasis () {
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date});
    const r = await fetch('/basis?' + q).then(r => r.json());
    state.basis_info = (r && !r.error) ? r : null;
  } catch (e) { state.basis_info = null; }
  updateSpotBox();   // refreshes index quote + contango chip together
}

function updateContango () {
  const el = document.getElementById('spot_basis');
  if (!el) return;
  const bi = state.basis_info;
  if (!bi || bi.basis == null) { el.textContent = 'basis —'; el.className = 'spot-basis stale'; return; }
  const sign = bi.basis > 0 ? '+' : '';
  el.textContent = `basis ${sign}${bi.basis.toFixed(1)}`;
  el.className = 'spot-basis' + (bi.fresh ? '' : ' stale');
  el.title = `Contango — futures ${bi.fut ?? '—'} − spot ${bi.spot ?? '—'} = ${sign}${bi.basis}`
           + (bi.fresh ? '' : ' (spot stale)');
}

// Heartbeat — rides the in-progress candle's tip. pulseHeartbeat() re-triggers
// the ripple on each live tick + updates the price tag; positionHeartbeat()
// pins it to the live (lastBk, lastClose) point in pixel space (so it tracks
// zoom/pan/scroll). Hidden on replay or when the tip scrolls out of view.
function pulseHeartbeat (price) {
  const hb = document.getElementById('heartbeat');
  if (!hb) return;
  if (price != null && state.tip) state.tip.y = price;   // tip follows latest tick
  if (price != null) updateSpotBox();                    // header FUTURE/INDEX live
  hb.classList.remove('stale', 'beat');
  void hb.offsetWidth;             // force reflow so the animation restarts
  hb.classList.add('beat');
  positionHeartbeat();
}

function positionHeartbeat () {
  const hb = document.getElementById('heartbeat');
  const gd = document.getElementById('chart');
  if (!hb || !gd) return;
  const fl = gd._fullLayout;
  if (isReplay() || !fl || !state.tip || !fl.xaxis || !fl.yaxis) {
    hb.style.display = 'none'; return;
  }
  const xa = fl.xaxis, ya = fl.yaxis;
  try {
    const x0 = xa.r2l(xa.range[0]), x1 = xa.r2l(xa.range[1]);
    const y0 = ya.r2l(ya.range[0]), y1 = ya.r2l(ya.range[1]);
    const x = state.tip.x, y = state.tip.y;
    if (x < Math.min(x0,x1) || x > Math.max(x0,x1) ||
        y < Math.min(y0,y1) || y > Math.max(y0,y1)) {
      hb.style.display = 'none'; return;     // tip scrolled out of view
    }
    hb.style.left = (xa._offset + xa.l2p(x)) + 'px';
    hb.style.top  = (ya._offset + ya.l2p(y)) + 'px';
    hb.style.display = 'flex';
  } catch (e) { hb.style.display = 'none'; }
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

  // These five endpoints are independent of one another, so fire them in
  // PARALLEL instead of serially — an instrument switch otherwise waits for
  // the SUM of all round-trips (snapshot + pivots + VP + option_levels +
  // basis ≈ 1.1 s) before the first repaint. Parallel = the max single call.
  const q = new URLSearchParams({inst: state.inst, date: state.date,
                                 tf: state.tf, cell: state.cell_size});
  const pq = new URLSearchParams({inst: state.inst, date: state.date});
  const [snap, pivots] = await Promise.all([
    fetch('/snapshot?' + q).then(r => r.json()).catch(() => null),
    fetch('/pivots?'   + pq).then(r => r.json()).catch(() => null),
    // fetchVolProfile / fetchOptionLevels / fetchBasis set state.* internally;
    // run them concurrently and don't block the two we destructure above.
    fetchVolProfile().catch(() => {}),
    fetchOptionLevels().catch(() => {}),
    fetchBasis().catch(() => {}),
  ]);
  state.snap = snap || {};
  state.n_ticks = state.snap.n_ticks || 0;
  rebuildIndex();
  // Pivots: prior-day classic floor pivots. Stable for the whole session.
  state.pivots = (pivots && !pivots.error) ? pivots : null;
  redrawAll();
  setStatus(isReplay() ? `replay · ${state.date}` : 'live ✓');
  // Heartbeat: live by default; on a replay there's no ticking, so park it.
  const hb = document.getElementById('heartbeat');
  const hbTag = document.getElementById('hb_tag');
  if (hb) {
    hb.classList.toggle('stale', isReplay());
    if (isReplay() && hbTag) hbTag.textContent = 'replay';
  }
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

function isReplay () { return state.date && state.today && state.date !== state.today; }

// Top-left header = the INDEX SPOT + live index % change (from the spot
// recorder via /basis). The near-month FUTURES price lives on the heartbeat
// tag instead. Driven by the 5s basis poll — light, not per-tick.
function updateSpotBox () {
  const bi = state.basis_info;
  // FUTURE = latest futures tick (state.tip.y, per tick); fallback to /basis fut.
  const fut = (state.tip && state.tip.y != null) ? state.tip.y
            : (bi && bi.fut != null ? bi.fut : null);
  // INDEX = futures − basis (per tick), anchored on the recorded index spot.
  let idx = (bi && bi.spot != null) ? bi.spot : null;
  if (bi && bi.basis != null && fut != null) idx = fut - bi.basis;

  const setRow = (pxId, chId, val, prevClose, fbPct, fbChg) => {
    const px = document.getElementById(pxId), ch = document.getElementById(chId);
    if (px) px.textContent = (val != null)
      ? val.toLocaleString('en-IN', {maximumFractionDigits: 1}) : '—';
    if (!ch) return;
    let pct = fbPct, chg = fbChg;
    if (val != null && prevClose) { chg = val - prevClose; pct = chg / prevClose * 100; }
    if (pct != null) {
      const s = pct > 0 ? '+' : '';
      ch.textContent = `${chg != null ? s + chg.toFixed(1) + ' ' : ''}(${s}${pct.toFixed(2)}%)`;
      ch.className = 'spot-chg ' + (pct > 0.001 ? 'up' : pct < -0.001 ? 'down' : 'flat');
    } else { ch.textContent = '—'; ch.className = 'spot-chg flat'; }
  };

  // INDEX % vs index prior close (= recorded spot − recorded day-change).
  const idxPrev = (bi && bi.spot != null && bi.change != null) ? bi.spot - bi.change : null;
  setRow('spot_px', 'spot_chg', idx, idxPrev, bi ? bi.change_pct : null, bi ? bi.change : null);
  // FUTURE % vs prior-day FUTURES close (pivots prior_day.close = futures series).
  const futPrev = (state.pivots && state.pivots.prior_day) ? state.pivots.prior_day.close : null;
  setRow('fut_px', 'fut_chg', fut, futPrev, null, null);

  updateContango();
}

async function fetchPositioning () {
  // The /positioning endpoint only knows LIVE state (flow/resting/INST/macro
  // update in real time). On a historical replay those numbers don't apply to
  // the date being viewed, so showing them would be misleading. Mark N/A.
  if (isReplay()) { renderPositioningReplay(); return; }
  try {
    const q = new URLSearchParams({inst: state.inst, date: state.date});
    const r = await fetch('/positioning?' + q).then(r => r.json());
    if (r && !r.error) {
      state.positioning = r;
      renderPositioning(r);
    }
  } catch (e) { /* ignore — next tick retries */ }
}

function renderPositioningReplay () {
  const panel = document.getElementById('positioning');
  if (panel) panel.classList.add('replay');
  ['pos_flow','pos_resting','pos_institutions','pos_macro','pos_composite']
    .forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove('bull','bear','flat'); el.classList.add('flat');
      const v = el.querySelector('.pos-value'); if (v) v.textContent = '—';
      const s = el.querySelector('.pos-sub');   if (s) s.textContent = 'live only';
    });
  const foot = document.getElementById('pos_updated');
  if (foot) foot.textContent = `replay ${state.date} · positioning is live-only`;
}

function renderPositioning (p) {
  const panel = document.getElementById('positioning');
  if (panel) panel.classList.remove('replay');
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
    // Option-OI levels shift as positioning builds intraday — re-fetch so the
    // CE-resistance / PE-support / max-pain lines track through the day.
    fetchOptionLevels();
    fetchBasis();
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
    if (m.type === 'ticks') {
      applyTicks(m.rows);
      if (m.rows && m.rows.length) pulseHeartbeat(m.rows[m.rows.length - 1].price);
    }
    if (m.type === 'depth') {
      state.depth = {ts_ms: m.ts_ms, bids: m.bids, asks: m.asks};
      state.depth_dirty = true;
    }
    // Feed-stall messages from the server-side WS streamer
    // (viewer/live_server.py emits these when no fresh ticks for ≥90 s
    // during market hours — the recorder is probably reconnecting).
    if (m.type === 'stall') {
      showStallBadge(m.stall_age_s);
      const hb = document.getElementById('heartbeat'); if (hb) hb.classList.add('stale');
    }
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

// ─── Pre-market / no-tick view ──────────────────────────────────────────────
// Draws the static reference levels (pivots, VP, option walls, last index
// spot) on a session-width frame so the chart is useful before 09:15 and for
// historical days with no tick capture. Persists for any date.
function renderPreMarket () {
  const piv = state.pivots && state.pivots.pivots;
  const vp  = state.vol_profile;
  const ol  = state.option_levels;
  const bi  = state.basis_info;
  const hb = document.getElementById('heartbeat'); if (hb) hb.style.display = 'none';

  if (!piv && !(vp && vp.poc != null)) {
    Plotly.react('chart', [{x:[], y:[], type:'scatter'}],
      {title:{text:'no levels available yet for this day',
              font:{color:'#8a93a0'}},
       paper_bgcolor:'#0f1318', plot_bgcolor:'#0f1318'},
      {responsive:true, displaylogo:false});
    return;
  }

  const cs = state.cell_size;
  const x0    = Date.parse(state.date + 'T09:00:00+05:30');
  const xOpen = Date.parse(state.date + 'T09:15:00+05:30');
  const x1    = Date.parse(state.date + 'T15:30:00+05:30');
  const xRange = [x0, x1];

  const shapes = [], annos = [], hov = [], ys = [];
  const addHover = (y, text) => {
    if (y == null) return;
    const N = 30, xs = [], yy = [], ts = [];
    for (let i = 0; i <= N; i++) { xs.push(x0 + (x1 - x0) * i / N); yy.push(y); ts.push(text); }
    hov.push({x:xs, y:yy, mode:'markers', marker:{size:12, color:'rgba(0,0,0,0)'},
              hoverinfo:'text', text:ts, xaxis:'x', yaxis:'y', showlegend:false});
  };
  const line = (y, color, dash, width) => {
    if (y == null) return;
    shapes.push({type:'line', xref:'x', yref:'y', x0:x0, x1:x1, y0:y, y1:y,
                 line:{color, width:width||1.2, dash:dash||'solid'}, layer:'below'});
    ys.push(y);
  };
  const label = (y, text, color, anchor) => {
    annos.push({x: anchor === 'right' ? x1 : x0, y, xref:'x', yref:'y', text,
                showarrow:false, xanchor:anchor||'left', yanchor:'middle',
                font:{size:10, color}});
  };

  if (piv) {
    [['R3',piv.R3,'#ff8a80','dot'],['R2',piv.R2,'#ef5350','dash'],
     ['R1',piv.R1,'#ef5350','solid'],['P',piv.P,'#fdd835','dash'],
     ['S1',piv.S1,'#26d0b0','solid'],['S2',piv.S2,'#26d0b0','dash'],
     ['S3',piv.S3,'#80cbc4','dot']].forEach(([k,v,c,d]) => {
      line(v, c, d, k==='P'?1.4:1.1); label(v, `<b>${k}</b> ${v.toFixed(1)}`, c, 'left');
      addHover(v, `${k} · floor pivot ${v.toFixed(1)}`);
    });
  }
  if (vp) {
    const sl = vp.scope === 'week' ? '5-day' : 'prior day';
    if (vp.poc != null) { line(vp.poc, '#a86ee0', 'solid', 1.8); addHover(vp.poc, `POC (${sl}) ${vp.poc.toFixed(0)} — fair value`); }
    if (vp.vah != null) { line(vp.vah, 'rgba(150,90,200,0.6)', 'dot', 0.9); addHover(vp.vah, `VAH (${sl}) ${vp.vah.toFixed(0)}`); }
    if (vp.val != null) { line(vp.val, 'rgba(150,90,200,0.6)', 'dot', 0.9); addHover(vp.val, `VAL (${sl}) ${vp.val.toFixed(0)}`); }
    (vp.hvn || []).forEach(y => { line(y, 'rgba(168,110,224,0.55)', 'solid', 1.1); addHover(y, `HVN ${y.toFixed(0)} — acceptance wall`); });
  }
  if (ol && ol.available) {
    const basis = (ol.basis != null) ? ol.basis : 0;
    [['CE-wall',ol.ce_resistance,'#ef5350','dash'],
     ['PE-wall',ol.pe_support,'#66bb6a','dash'],
     ['MaxPain',ol.max_pain,'#ff8f00','dot']].forEach(([nm,px,c,d]) => {
      if (px == null) return;
      const y = px + basis;
      line(y, c, d, 1.3);
      label(y, `<b>${nm}</b> ${px.toFixed(0)}${basis?`→${y.toFixed(0)}`:''}`, c, 'right');
      addHover(y, `${nm} strike ${px.toFixed(0)} · option-OI`);
    });
  }
  if (bi && bi.spot != null) {
    line(bi.spot, '#e6e9ee', 'dash', 1.4);
    const pct = (bi.change_pct != null) ? ` (${bi.change_pct>0?'+':''}${bi.change_pct.toFixed(2)}%)` : '';
    label(bi.spot, `<b>spot</b> ${bi.spot.toFixed(1)}${pct}`, '#e6e9ee', 'left');
    addHover(bi.spot, `Last index spot ${bi.spot.toFixed(1)}${pct} — pre-open`);
    hov.push({x:[xOpen], y:[bi.spot], mode:'markers',
              marker:{size:9, color:'#e6e9ee', line:{color:'#0f1318', width:1}},
              hoverinfo:'text', text:[`spot ${bi.spot.toFixed(1)}`],
              xaxis:'x', yaxis:'y', showlegend:false});
  }

  const lo = ys.length ? Math.min(...ys) : 0;
  const hi = ys.length ? Math.max(...ys) : 1;
  const pad = (hi - lo) * 0.08 + cs * 2;
  const yRange = [lo - pad, hi + pad];

  annos.push({x:x0, y:hi + pad*0.5, xref:'x', yref:'y',
    text:`PRE-OPEN · levels as of 09:00–09:15 · ${state.date}`,
    showarrow:false, xanchor:'left', yanchor:'top',
    font:{size:11, color:'#8a93a0'}});

  const GRID = 'rgba(255,255,255,0.05)';
  const layout = {
    paper_bgcolor:'#0f1318', plot_bgcolor:'#0f1318', font:{color:'#aeb6c2', size:11},
    margin:{l:55, r:15, t:18, b:38}, showlegend:false, hovermode:'closest',
    xaxis:{type:'date', range:xRange, tickformat:'%H:%M', gridcolor:GRID, zeroline:false},
    yaxis:{range:yRange, title:'price', tickformat:',d', gridcolor:GRID, zeroline:false},
    shapes, annotations:annos,
    hoverlabel:{bgcolor:'#161b22', bordercolor:'#2a313b', font:{color:'#e6e9ee', size:11}},
    uirevision:`premarket|${state.inst}|${state.date}`,
  };
  const anchor = [{x:[x0, x1], y:[yRange[0], yRange[1]], mode:'markers',
                   marker:{size:0.1, color:'rgba(0,0,0,0)'}, hoverinfo:'skip',
                   xaxis:'x', yaxis:'y'}];
  Plotly.react('chart', anchor.concat(hov), layout, {responsive:true, displaylogo:false});
}

// ─── Drawing — Plotly figure ────────────────────────────────────────────────
function redrawAll () {
  const candles = [...state.candles_idx.values()].sort((a,b) => a.bucket - b.bucket);
  const cells   = state.snap.cells;
  if (candles.length === 0) {
    // No ticks yet (pre-open) OR a historical day we never recorded ticks for.
    // Show the reference levels we DO have (pivots / VP / option walls / last
    // index spot) so the chart is useful from 09:00, not blank.
    renderPreMarket();
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

  // Hover hit-areas for level LINES. Plotly shapes can't carry hover text, so
  // for every level (pivots, POC/VAH/VAL, HVN, naked POC, option walls) we add
  // a fat transparent scatter line that shows a tooltip describing the level.
  // Lets us drop on-chart labels (HVN, POC) yet still explain every line.
  const hoverTraces = [];
  const addLevelHover = (y, text) => {
    if (y == null) return;
    // Many invisible marker points along the line so 'closest' hover snaps to
    // one anywhere along it (a 2-point line only triggers near its endpoints).
    const N = 30, xs = [], ys = [], ts = [];
    for (let i = 0; i <= N; i++) {
      xs.push(xRange[0] + (xRange[1] - xRange[0]) * i / N);
      ys.push(y); ts.push(text);
    }
    hoverTraces.push({
      x: xs, y: ys, mode: 'markers',
      marker: {size: 12, color: 'rgba(0,0,0,0)'},
      hoverinfo: 'text', text: ts,
      xaxis: 'x', yaxis: 'y', showlegend: false,
    });
  };

  // value-area band per candle
  for (const c of candles) {
    shapes.push({
      type:'rect', xref:'x', yref:'y',
      x0:c.bucket - half_w, x1:c.bucket + half_w,
      y0:c.val - cs/2,      y1:c.vah + cs/2,
      fillcolor:'rgba(80,180,120,0.07)', line:{width:0}, layer:'below',
    });
  }

  // cells
  for (const c of cells) {
    const m = maxes.get(c.bucket) || {buy:1, sell:1};
    const sv = useQty ? c.sell_qty : c.sell_ticks;
    const bv = useQty ? c.buy_qty  : c.buy_ticks;
    const s_int = sv / (m.sell || 1);
    const b_int = bv / (m.buy  || 1);
    // Dark-mode footprint colours: SELL = red (left), BUY = green (right).
    // Intensity ∝ volume; faint base so empty cells are barely-there. Light
    // hairline borders (a black border was invisible/inverted on dark).
    const sCol = sv > 0 ? `rgba(239,83,80,${(0.16 + 0.72*s_int).toFixed(2)})`
                        : 'rgba(239,83,80,0.05)';
    const bCol = bv > 0 ? `rgba(38,200,140,${(0.16 + 0.72*b_int).toFixed(2)})`
                        : 'rgba(38,200,140,0.05)';
    const cellBorder = 'rgba(255,255,255,0.12)';

    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket - half_w, x1:c.bucket - body_w,
      y0:c.cell - cs/2,     y1:c.cell + cs/2,
      fillcolor:sCol, line:{width:0.3, color:cellBorder}});
    shapes.push({type:'rect', xref:'x', yref:'y',
      x0:c.bucket + body_w, x1:c.bucket + half_w,
      y0:c.cell - cs/2,     y1:c.cell + cs/2,
      fillcolor:bCol, line:{width:0.3, color:cellBorder}});

    // Imbalance contour — colour by side so it's intuitive (green = stacked
    // buying, red = stacked selling) rather than a neutral amber.
    if (c.imbalance === 'BUY') {
      shapes.push({type:'rect', xref:'x', yref:'y',
        x0:c.bucket + body_w, x1:c.bucket + half_w,
        y0:c.cell - cs/2,     y1:c.cell + cs/2,
        fillcolor:'rgba(0,0,0,0)', line:{width:1.6, color:'#4dffce'}});
    } else if (c.imbalance === 'SELL') {
      shapes.push({type:'rect', xref:'x', yref:'y',
        x0:c.bucket - half_w, x1:c.bucket - body_w,
        y0:c.cell - cs/2,     y1:c.cell + cs/2,
        fillcolor:'rgba(0,0,0,0)', line:{width:1.6, color:'#ff5a5a'}});
    }

    annos.push({
      x: c.bucket - (half_w + body_w)/2, y: c.cell,
      xref:'x', yref:'y', showarrow:false,
      text: c.imbalance === 'SELL' ? `<b>${fmtNum(sv)}</b>` : fmtNum(sv),
      font:{size:7, color: sv > 0 ? '#ff7b72' : '#5a6472'},
    });
    annos.push({
      x: c.bucket + (half_w + body_w)/2, y: c.cell,
      xref:'x', yref:'y', showarrow:false,
      text: c.imbalance === 'BUY'  ? `<b>${fmtNum(bv)}</b>` : fmtNum(bv),
      font:{size:7, color: bv > 0 ? '#3fd6c4' : '#5a6472'},
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

  // Experimental badges only in Analysis mode (kept off the disciplined
  // Clean cockpit). Computations above stay so the entry-scorer can reuse them.
  for (let i = 0; state.analysis_mode && i < sortedC.length; i++) {
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
  // Window by TIME span, not candle count. With intraday gaps (e.g. a data
  // outage) the candle count is fewer than the number of time-slots, so a
  // count-based window comes out too narrow and clips the START of the
  // session. At zoom_x=1 this spans firstBk→lastBk (the WHOLE session);
  // zoom_x>1 narrows to the most-recent fraction of the day. Applied to the
  // axis only when applyRange is true (follow mode or a button's force_range);
  // native drag/pan is preserved otherwise.
  const fullSpan = Math.max(tf_ms, lastBk - firstBk);
  const visSpan  = fullSpan / state.zoom_x;
  const startBk  = Math.max(firstBk, lastBk - visSpan);
  const xRange = [startBk - tf_ms * 0.5, lastBk + tf_ms * 0.5];

  // y: fit the FULL high-low range of the candles in view, centred on the
  // DATA midpoint — not the last close, which off-centres the window and
  // clips intraday extremes (e.g. a late-session low). zoom_y contracts span.
  const visibleCandles = candles.filter(c => c.bucket >= startBk);
  const yLo0 = Math.min(...visibleCandles.map(c => c.low));
  const yHi0 = Math.max(...visibleCandles.map(c => c.high));
  const yMid = (yLo0 + yHi0) / 2;
  const yHalfSpan = ((yHi0 - yLo0) / 2 + 2 * cs) / state.zoom_y;
  const yRange = [yMid - yHalfSpan, yMid + yHalfSpan];
  const visCandles = visibleCandles.length;

  state.tip = {x: lastBk, y: candles[candles.length - 1].close};
  updateSpotBox();   // tick-live: tracks the latest futures close − basis

  // ── Pivot lines (classic floor pivots from prior day's OHLC) ──────────
  // Drawn AFTER xRange/yRange are defined. Levels outside the visible
  // y-window are skipped so labels don't hang in space.
  if (state.pivots && state.pivots.pivots) {
    const piv = state.pivots.pivots;
    const levels = [
      {k:'R3', v:piv.R3, color:'#ff8a80', dash:'dot',    weight:0.9},
      {k:'R2', v:piv.R2, color:'#ef5350', dash:'dash',   weight:1.0},
      {k:'R1', v:piv.R1, color:'#ef5350', dash:'solid',  weight:1.2},
      {k:'P',  v:piv.P,  color:'#fdd835', dash:'dash',   weight:1.4},
      {k:'S1', v:piv.S1, color:'#26d0b0', dash:'solid',  weight:1.2},
      {k:'S2', v:piv.S2, color:'#26d0b0', dash:'dash',   weight:1.0},
      {k:'S3', v:piv.S3, color:'#80cbc4', dash:'dot',    weight:0.9},
    ];
    for (const L of levels) {
      if (L.v < yRange[0] - cs || L.v > yRange[1] + cs) continue;
      shapes.push({
        type:'line', xref:'x', yref:'y',
        x0: xRange[0], x1: xRange[1], y0: L.v, y1: L.v,
        line: {color: L.color, width: L.weight, dash: L.dash},
        layer: 'below',
      });
      // No background box — just crisp coloured text on the dark chart.
      annos.push({
        x: xRange[0], y: L.v, xref:'x', yref:'y',
        text: `<b>${L.k}</b> ${L.v.toFixed(1)}`,
        showarrow:false, xanchor:'left', yanchor:'middle',
        font:{size:10, color:L.color},
      });
      const pivNames = {P:'Pivot (P)', R1:'Resistance 1', R2:'Resistance 2',
        R3:'Resistance 3', S1:'Support 1', S2:'Support 2', S3:'Support 3'};
      addLevelHover(L.v, `${pivNames[L.k] || L.k} · floor pivot ${L.v.toFixed(1)}`);
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
    // Volume-profile family (POC / VAH-VAL / naked POC / HVN / LVN gap) all
    // share ONE left indent so they read as one group — distinct from the
    // pivot family (hard left edge) and the option family (right edge).
    const vpIndent = xRange[0] + (xRange[1] - xRange[0]) * 0.06;

    // Value-area band (translucent purple) between VAL and VAH
    if (vp.val != null && vp.vah != null) {
      shapes.push({
        type:'rect', xref:'x', yref:'y', layer:'below',
        x0: xRange[0], x1: xRange[1], y0: vp.val, y1: vp.vah,
        fillcolor:'rgba(150,90,200,0.10)', line:{width:0},
      });
    }
    // POC — bold purple line (no on-chart label; hover explains it)
    if (vp.poc != null && inView(vp.poc)) {
      shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
        x0:xRange[0], x1:xRange[1], y0:vp.poc, y1:vp.poc,
        line:{color:'#7b1fa2', width:1.8}});
      addLevelHover(vp.poc,
        `POC (${scopeLbl==='wk'?'5-day':'prior day'}) ${vp.poc.toFixed(0)} — fair value / heaviest acceptance`);
    }
    // VAH / VAL dashed bounds
    [['VAH', vp.vah], ['VAL', vp.val]].forEach(([lbl, y]) => {
      if (y != null && inView(y)) {
        shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
          x0:xRange[0], x1:xRange[1], y0:y, y1:y,
          line:{color:'rgba(123,31,162,0.5)', width:0.8, dash:'dot'}});
        addLevelHover(y, `${lbl} (${scopeLbl==='wk'?'5-day':'prior day'}) ${y.toFixed(0)} — value-area ${lbl==='VAH'?'high (sellers)':'low (buyers)'}`);
      }
    });
    // HVN acceptance walls — purple lines, fainter/thinner than the bold POC.
    // Shown in BOTH modes (legit S/R); no label, hover explains. Top-6 only.
    (vp.hvn || []).forEach(y => {
      if (!inView(y)) return;
      shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
        x0:xRange[0], x1:xRange[1], y0:y, y1:y,
        line:{color:'rgba(168,110,224,0.55)', width:1.1}});
      addLevelHover(y, `HVN ${y.toFixed(0)} — high-volume node / acceptance wall (hard to break)`);
    });
    // ↓↓↓ Experimental / detail overlays — Analysis mode only. ↓↓↓
    // (POC + value-area band above stay in Clean mode as key levels.)
    if (state.analysis_mode) {
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
        annos.push({x:vpIndent, y:pp.poc, xref:'x', yref:'y',
          text:`nPOC ${pp.date.slice(5)}`,
          showarrow:false, xanchor:'left', yanchor:'bottom',
          bgcolor:'rgba(255,255,255,0.8)', bordercolor:'#ff6f00', borderwidth:1,
          font:{size:8, color:'#ff6f00'}, borderpad:1});
        addLevelHover(pp.poc, `Naked POC ${pp.date.slice(5)} ${pp.poc.toFixed(0)} — untested prior POC (price magnet)`);
      }
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
        x: vpIndent, y: (z.lo + z.hi) / 2,
        xref:'x', yref:'y',
        xanchor:'left', yanchor:'middle', text:'▹ gap',
        showarrow:false, font:{size:8, color:'#999'},
        hovertext:`LVN vacuum ${z.lo.toFixed(0)}-${z.hi.toFixed(0)} — `
                + `thin volume, price slices through fast (no support here)`,
      });
    });
    }  // end if (state.analysis_mode) — experimental VP overlays
  }

  // ── Option-OI institutional levels (Clean mode — these are KEY) ────────
  // CE-resistance (call wall), PE-support (put wall), Max Pain magnet.
  // Institutional positioning levels — shown in Clean mode alongside pivots
  // because they're standard/validated, not experimental heuristics. They
  // re-poll every 5s so the lines TRACK as OI builds through the day.
  if (state.option_levels && state.option_levels.available) {
    const ol = state.option_levels;
    const inView2 = (y) => y != null && y >= yRange[0] - cs && y <= yRange[1] + cs;
    const staleTag = ol.stale ? ` ⚠stale ${ol.day_used.slice(5)}` : '';
    // Contango: walls are SPOT strikes; chart is FUTURES. Map each strike onto
    // the futures axis by adding the (server-guarded) basis = futures − spot.
    // If basis is null (spot missing/stale/insane) we plot at the raw strike.
    const basisAdj = (ol.basis != null);
    const basis = basisAdj ? ol.basis : 0;
    const bSign = basis > 0 ? '+' : '';
    const basisNote = basisAdj
      ? ` · basis-adj ${bSign}${basis.toFixed(0)} (strike→fut)`
      : ' · basis n/a (shown at strike)';
    const olLevels = [
      {px: ol.ce_resistance, name: 'CE-wall', color: '#c62828', dash: 'dash',
       note: 'call-OI resistance (writers defend above)'},
      {px: ol.pe_support,    name: 'PE-wall', color: '#2e7d32', dash: 'dash',
       note: 'put-OI support (writers defend below)'},
      {px: ol.max_pain,      name: 'MaxPain', color: '#ff8f00', dash: 'dot',
       note: 'max-pain magnet (writers lose least here)'},
    ];
    for (const L of olLevels) {
      if (L.px == null) continue;
      const yAdj = L.px + basis;            // futures-mapped level
      if (!inView2(yAdj)) continue;
      shapes.push({type:'line', xref:'x', yref:'y', layer:'below',
        x0:xRange[0], x1:xRange[1], y0:yAdj, y1:yAdj,
        line:{color:L.color, width:1.3, dash:L.dash}});
      addLevelHover(yAdj,
        `${L.name} strike ${L.px.toFixed(0)}${basisAdj?` → fut ${yAdj.toFixed(0)}`:''} — ${L.note} · PCR ${ol.pcr_oi}${basisNote}`);
      annos.push({x:xRange[1], y:yAdj, xref:'x', yref:'y',
        text:`<b>${L.name}</b> ${L.px.toFixed(0)}${basisAdj?`→${yAdj.toFixed(0)}`:''}${L.name==='CE-wall'?staleTag:''}`,
        showarrow:false, xanchor:'right', yanchor:'top',
        bgcolor:'rgba(255,255,255,0.85)', bordercolor:L.color, borderwidth:1,
        font:{size:9, color:L.color}, borderpad:2,
        hovertext:`${L.note} · PCR ${ol.pcr_oi}`});
    }
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
  if (state.analysis_mode && state.vol_profile && candles.length >= 3) {
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
  // Per-level buy/sell numbers (both sides) + an OVERALL total per level.
  const profSellLabels = prof.map(p => fmtNum(profUseQty ? p.sell_qty : p.sell_ticks));
  const profBuyLabels  = prof.map(p => fmtNum(profUseQty ? p.buy_qty  : p.buy_ticks));
  const profNet = prof.map(p => profUseQty ? (p.buy_qty - p.sell_qty)
                                           : (p.buy_ticks - p.sell_ticks));
  const profVol = prof.map(p => profUseQty ? (p.buy_qty + p.sell_qty)
                                           : (p.buy_ticks + p.sell_ticks));
  // x2 axis bound — right-anchors the NET column inside the BS pane.
  const _profMax = Math.max(1, ...profBuy, ...profSell.map(Math.abs));
  const prof2Max = _profMax * 1.15;
  // NET (buy − sell) per price level — signed + colour-coded, right column.
  prof.forEach((p, i) => {
    if (profVol[i] <= 0) return;
    const net = profNet[i];
    const sign = net > 0 ? '+' : net < 0 ? '−' : '';
    annos.push({
      x: prof2Max * 0.98, y: p.cell, xref:'x2', yref:'y2',
      text: `<b>${sign}${fmtNum(Math.abs(net))}</b>`, showarrow:false,
      xanchor:'right', yanchor:'middle',
      font:{size:7, color: net > 0 ? '#3fd6c4' : net < 0 ? '#ff7b72' : '#8a93a0'},
    });
  });

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

  // Dark theme palette (kept here so chart + CSS stay in sync).
  const GRID = 'rgba(255,255,255,0.05)';
  const layout = {
    paper_bgcolor:'#0f1318', plot_bgcolor:'#0f1318',
    font:{color:'#aeb6c2', size:11},
    margin:{l:55, r:15, t:18, b:38},
    showlegend:false,
    // domains: main=72%, session profile=12%, live DOM=14% (right edge).
    xaxis:  {type:'date', domain:[0, 0.72], ...xRangeOpt,
             dtick: tf_ms * xLabelStride, tickformat:'%H:%M', tick0: lastBk,
             gridcolor:GRID, gridwidth:1, showspikes:false, zeroline:false},
    yaxis:  {domain:[0.32, 1], title:'price', side:'left', ...yRangeOpt,
             dtick: yDtick, tickformat:',d',
             gridcolor:GRID, gridwidth:1, zeroline:false},
    // Session BS-summary pane — more vertical gridlines (nticks) for scale.
    xaxis2: {domain:[0.73, 0.84], anchor:'y2', title:'BS qty',
             showticklabels:true, tickfont:{size:9}, nticks:6,
             range:[-prof2Max, prof2Max],
             gridcolor:GRID, zeroline:true, zerolinecolor:'rgba(255,255,255,0.18)'},
    yaxis2: {domain:[0.32, 1], matches:'y', showticklabels:false},
    // Delta pane — pushed lower (bigger gap above) + matches:'x' so it mirrors
    // the candle pane's zoom/pan in lockstep.
    xaxis3: {type:'date', domain:[0, 0.72], anchor:'y3', matches:'x',
             tickformat:'%H:%M', gridcolor:GRID,
             showticklabels:true, tickfont:{size:9}},
    yaxis3: {domain:[0, 0.18], title:'Δ qty', gridcolor:GRID,
             tickfont:{size:9}, nticks:4, zeroline:true,
             zerolinecolor:'rgba(255,255,255,0.18)'},
    // Live DOM + session profile pane — more vertical gridlines.
    xaxis4: {domain:[0.86, 1.0], anchor:'y4', title:'DOM (live + session)',
             showticklabels:true, tickfont:{size:9}, nticks:6,
             zeroline:true, zerolinecolor:'rgba(255,255,255,0.18)', zerolinewidth:1,
             gridcolor:GRID},
    yaxis4: {domain:[0.32, 1], matches:'y', showticklabels:false},
    shapes, annotations: annos,
    barmode:'overlay',
    uirevision: uirev,
    hovermode:'closest',
    modebar:{bgcolor:'rgba(0,0,0,0)', color:'#6b7480', activecolor:'#e6e9ee',
             orientation:'v'},
    hoverlabel:{bgcolor:'#161b22', bordercolor:'#2a313b',
                font:{color:'#e6e9ee', size:11}},
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
     text: profSellLabels,
     textposition:'inside', insidetextanchor:'end', cliponaxis:false,
     textfont:{size:9, color:'#fff'},
     hovertemplate:'price=%{y}<br>sell=%{x:.0f}<extra></extra>'},
    // session profile (pane #2) — buy side (right of axis, positive x)
    {x: profBuy,  y: profY, type:'bar', orientation:'h',
     xaxis:'x2', yaxis:'y2',
     marker:{color:'rgba(38,166,154,0.85)'},
     text: profBuyLabels,
     textposition:'inside', insidetextanchor:'start', cliponaxis:false,
     textfont:{size:9, color:'#fff'},
     hovertemplate:'price=%{y}<br>buy=%{x:.0f}<extra></extra>'},
    // delta histogram (bottom pane) — numeric labels on top of positive bars,
    // bottom of negative bars (Plotly's 'outside' handles this automatically).
    {x: dx, y: dy, type:'bar', marker:{color: dcol},
     xaxis:'x3', yaxis:'y3',
     text: dy.map(v => fmtNum(Math.abs(v))),
     textposition:'outside', cliponaxis:false,
     textfont:{size:9, color:'#aeb6c2'},
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
  // Transparent hover hit-areas for every level line (added last so they sit
  // on top and reliably catch the cursor).
  traces.push(...hoverTraces);

  Plotly.react('chart', traces, layout,
               {responsive:true, displaylogo:false, scrollZoom:true});
  positionHeartbeat();   // pin the live pulse to the candle tip after redraw

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
        positionHeartbeat();   // keep the live pulse glued to the tip on pan/zoom
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
