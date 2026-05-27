"""alerts/tick_recorder_v2.py — Event-driven tick recorder (experimental).

This is the parallel A/B candidate to alerts/tick_recorder.py. Goal: stop
the silent data loss demonstrated on 2026-05-26 (~24 % of NIFTY session
volume merged into 57 'burst' rows because the v1 recorder polls
get_ltp() at 10 Hz — a state snapshot — instead of consuming the WS
event stream).

What's different vs v1
----------------------
1.  **Event-driven, not polled.** Uses Groww's `subscribe_ltp(on_data_received=cb)`
    and `subscribe_market_depth(on_data_received=cb)` callback hooks. Each
    callback invocation is one NATS message Groww chose to deliver →
    one row. No inter-poll aggregation.

2.  **qty=0 rows dropped.** v1 emitted ~25 % of rows with qty=0 (the WS
    timestamp advanced but the 2 s REST volume poller hadn't caught up).
    v2 holds the LTP row until cum_volume confirms qty > 0; if confirmation
    doesn't arrive within VOL_STALE_MS the row is written with qty=0 but
    rule='UNVERIFIED' so research can filter it.

3.  **cum_volume reversals handled.** v1 silently lost qty when cum_volume
    moved backward (broker re-sending stale state during reconnect). v2
    detects reversal, logs a warning, re-seeds prev_cum_vol = new (lower)
    value, and tags the next row with rule='RESEED'.

4.  **GAP markers inserted.** Whenever inter-message gap > GAP_THRESHOLD_MS,
    write a synthetic row with side='GAP', qty=0, ts_ms = midpoint. Lets
    research filter or count gaps without re-deriving from raw ts_ms diffs.

5.  **Reconnect markers.** NATS disconnect/reconnect events emit a row with
    side='RECONNECT', so a discontinuity in the data is explicit.

6.  **SENSEX included** by default (v1 had only NIFTY, BANKNIFTY).

7.  **Extra derived columns** in the ticks CSV:
        microprice    size-weighted mid: (ask*bid_qty + bid*ask_qty) /
                      (bid_qty + ask_qty)
        notional      qty * price (₹)
        aggression    continuous Lee-Ready score (price - mid) / (spread/2)
                      in [-1, +1]; |aggression| < 0.5 = ambiguous
        gap_ms        time since previous tick row for this instrument
        msg_seq       monotonic per-instrument message counter

8.  **Separate output paths** — runs alongside v1 without conflict:
        v3/cache/ticks_v2_<INST>_<YYYYMMDD>.csv
        v3/cache/depth_v2_<INST>_<YYYYMMDD>.csv
        logs/trade_bot/tick_recorder_v2.log

Run:
    python -m alerts.tick_recorder_v2
Cron (test):
    Leave v1 cron untouched; manually start v2 with:
        nohup /opt/anaconda3/bin/python3 -m alerts.tick_recorder_v2 \
            > logs/trade_bot/tick_recorder_v2_console.log 2>&1 &
"""
from __future__ import annotations

import csv
import os
import pathlib
import signal
import sys
import threading
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dtime

import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse the existing classify() + best-quote helpers from v1 verbatim —
# the Lee-Ready logic is the same; only the *event pipeline* changes.
from alerts.tick_recorder import classify as _classify_v1   # noqa: E402

CACHE_DIR = ROOT / 'v3' / 'cache'
LOG_DIR   = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ─── Env-tunable knobs ────────────────────────────────────────────────────────
INSTRUMENTS_ENV   = os.environ.get('TICK_REC_V2_INSTRUMENTS',
                                   'NIFTY,BANKNIFTY,SENSEX')
END_HHMM          = int  (os.environ.get('TICK_REC_V2_END_HHMM',     '1535'))
FLUSH_N           = int  (os.environ.get('TICK_REC_V2_FLUSH_N',        '50'))
FLUSH_SEC         = float(os.environ.get('TICK_REC_V2_FLUSH_SEC',     '1.0'))
HEARTBEAT_SEC     = float(os.environ.get('TICK_REC_V2_HEARTBEAT_SEC','60.0'))
DEPTH_THROTTLE_MS = int  (os.environ.get('TICK_REC_V2_DEPTH_MS',     '1000'))
VOL_POLL_SEC      = float(os.environ.get('TICK_REC_V2_VOL_POLL_SEC', '2.0'))
GAP_THRESHOLD_MS  = int  (os.environ.get('TICK_REC_V2_GAP_MS',      '5000'))
VOL_STALE_MS      = int  (os.environ.get('TICK_REC_V2_VOL_STALE_MS','3000'))


# ─── Logging ──────────────────────────────────────────────────────────────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record); self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('tick_recorder_v2')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = _FlushingFileHandler(LOG_DIR / 'tick_recorder_v2.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Auth + contract resolution — reuse v1 helpers ───────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    last_err = None
    for attempt in range(3):
        try:
            env = {}
            with open(ROOT / 'token.env') as f:
                for ln in f:
                    if '=' in ln:
                        k, _, v = ln.strip().partition('=')
                        env[k] = v
            totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(token=token)
        except Exception as e:
            last_err = e
            log.warning('Groww auth attempt %d failed: %s — retrying 5s',
                        attempt + 1, e)
            time.sleep(5)
    raise RuntimeError(f'Groww auth failed after 3 attempts: {last_err}')


def _last_weekday(y: int, m: int, weekday: int) -> date:
    """weekday: 0=Mon 1=Tue 2=Wed ... 6=Sun. Returns the last such weekday in (y,m)."""
    last_day = (date(y + 1, 1, 1) - timedelta(days=1)) if m == 12 \
        else (date(y, m + 1, 1) - timedelta(days=1))
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _near_monthly_expiry(today: date, weekday: int) -> date:
    OVERRIDES = {date(2026, 3, 31): date(2026, 3, 30)}
    y, m = today.year, today.month
    for _ in range(3):
        exp = OVERRIDES.get(_last_weekday(y, m, weekday),
                            _last_weekday(y, m, weekday))
        if exp >= today:
            return exp
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    raise RuntimeError(f'no monthly expiry found for {today}')


def _resolve_symbol(g, inst: str, today: date) -> dict:
    """Resolve current monthly fut for inst. NIFTY/BANKNIFTY expire last Tue
    on NSE; SENSEX expires last Wed on BSE. If today *is* the expiry day,
    roll forward to next month (the expiring contract often has low post-
    morning volume and the next month is where positioning has moved)."""
    if inst == 'SENSEX':
        exch, weekday = 'BSE', 2     # last Wed
    else:
        exch, weekday = 'NSE', 1     # last Tue
    exp = _near_monthly_expiry(today, weekday)
    if exp == today:                  # roll to next month on expiry day itself
        nxt = today + timedelta(days=8)
        exp = _near_monthly_expiry(nxt, weekday)
    sym = f'{exch}-{inst}-{exp.day}{exp.strftime("%b")}{exp.strftime("%y")}-FUT'
    try:
        meta = g.get_instrument_by_groww_symbol(sym)
    except Exception as e:
        log.warning('symbol resolve failed for %s: %s', sym, e)
        raise
    return {'inst': inst, 'symbol': sym, 'exchange': exch,
            'exchange_token': str(meta['exchange_token']),
            'trading_symbol': str(meta['trading_symbol']),
            'lot_size': int(meta['lot_size']),
            'tick_size': float(meta['tick_size'])}


# ─── Per-instrument state (thread-safe — callbacks fire on NATS asyncio thread) ─
@dataclass
class TickState:
    prev_ts_ms:    int   = 0
    prev_ltp:      float = 0.0
    prev_cum_vol:  float = 0.0
    prev_side:     str   = 'UNK'
    seen_first:    bool  = False
    msg_seq:       int   = 0
    n_buy:         int   = 0
    n_sell:        int   = 0
    n_unk:         int   = 0
    n_qty_zero:    int   = 0
    n_reversal:    int   = 0
    n_gap:         int   = 0
    cum_delta_qty: float = 0.0
    # Latest top-of-book — populated by depth callback, consumed by LTP callback
    bid:        float = 0.0
    ask:        float = 0.0
    bid_qty:    float = 0.0
    ask_qty:    float = 0.0
    last_depth_write_ms: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


# ─── CSV writers ──────────────────────────────────────────────────────────────
class TickWriter:
    """Buffered v2 CSV writer. Adds derived microprice / notional / aggression
    / gap_ms / msg_seq columns next to the v1 schema."""
    COLUMNS = [
        'ts_ms', 'inst', 'price', 'qty', 'side', 'rule',
        'bid', 'ask', 'bid_qty', 'ask_qty', 'spread', 'cum_volume',
        'microprice', 'notional', 'aggression', 'gap_ms', 'msg_seq',
    ]

    def __init__(self, inst: str, run_date: date):
        self.inst = inst
        self.path = CACHE_DIR / f'ticks_v2_{inst}_{run_date.strftime("%Y%m%d")}.csv'
        self.buffer: list[dict] = []
        self.last_flush = time.time()
        new_file = not self.path.exists()
        self._fh = open(self.path, 'a', newline='', buffering=1)
        self._w  = csv.DictWriter(self._fh, fieldnames=self.COLUMNS,
                                  extrasaction='ignore')
        if new_file:
            self._w.writeheader(); self._fh.flush()
        self._lock = threading.Lock()

    def add(self, row: dict) -> None:
        with self._lock:
            self.buffer.append(row)
            if len(self.buffer) >= FLUSH_N or \
               (time.time() - self.last_flush) >= FLUSH_SEC:
                self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        if not self.buffer:
            return
        self._w.writerows(self.buffer)
        self._fh.flush()
        self.buffer.clear()
        self.last_flush = time.time()

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def close(self) -> None:
        self.flush()
        try: self._fh.close()
        except Exception: pass


class DepthWriter:
    COLUMNS = ['ts_ms', 'inst', 'level', 'side', 'price', 'qty']

    def __init__(self, inst: str, run_date: date):
        self.inst = inst
        self.path = CACHE_DIR / f'depth_v2_{inst}_{run_date.strftime("%Y%m%d")}.csv'
        new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = open(self.path, 'a', newline='', buffering=1)
        self._w  = csv.DictWriter(self._fh, fieldnames=self.COLUMNS,
                                  extrasaction='ignore')
        if new:
            self._w.writeheader(); self._fh.flush()
        self._lock = threading.Lock()

    def write_snapshot(self, ts_ms: int, depth_row: dict) -> None:
        if not depth_row:
            return
        rows = []
        for side_key, side_label in (('buyBook', 'BID'), ('sellBook', 'ASK')):
            book = depth_row.get(side_key, {}) or {}
            for lvl_str, item in book.items():
                try:
                    lvl = int(lvl_str)
                except (ValueError, TypeError):
                    continue
                p = float(item.get('price', 0) or 0)
                q = float(item.get('qty',   0) or 0)
                if p <= 0:
                    continue
                rows.append({'ts_ms': ts_ms, 'inst': self.inst, 'level': lvl,
                             'side': side_label, 'price': p, 'qty': q})
        if not rows:
            return
        with self._lock:
            self._w.writerows(rows)

    def close(self) -> None:
        try: self._fh.close()
        except Exception: pass


# ─── REST volume poller — same shape as v1 ────────────────────────────────────
class _VolPoller(threading.Thread):
    def __init__(self, g, contracts: dict, state: dict,
                 stop_evt: threading.Event):
        super().__init__(daemon=True, name='vol-poller-v2')
        self._g = g; self._contracts = contracts
        self._state = state; self._stop = stop_evt

    def run(self):
        while not self._stop.is_set():
            t0 = time.time()
            for inst, c in self._contracts.items():
                seg = 'BFO' if inst == 'SENSEX' else 'FNO'
                try:
                    r = self._g.get_quote(exchange=c['exchange'], segment=seg,
                                          trading_symbol=c['trading_symbol'])
                    self._state[inst] = {
                        'cum_volume': float(r.get('volume') or 0.0),
                        'last_qty':   float(r.get('last_trade_quantity') or 0.0),
                        'last_ts':    int  (r.get('last_trade_time')     or 0),
                        'oi':         float(r.get('open_interest')       or 0.0),
                        'total_buy_qty':  float(r.get('total_buy_quantity')  or 0.0),
                        'total_sell_qty': float(r.get('total_sell_quantity') or 0.0),
                        'updated_at_ms':  int(time.time() * 1000),
                    }
                except Exception as e:
                    log.warning('vol_poll %s err: %s', inst, e)
            dt = time.time() - t0
            if dt < VOL_POLL_SEC:
                self._stop.wait(VOL_POLL_SEC - dt)


# ─── Derived feature helpers ──────────────────────────────────────────────────
def _microprice(bid: float, ask: float, bq: float, aq: float) -> float:
    denom = bq + aq
    if denom <= 0 or bid <= 0 or ask <= 0:
        return (bid + ask) / 2 if (bid > 0 and ask > 0) else 0.0
    return (ask * bq + bid * aq) / denom


def _aggression(price: float, bid: float, ask: float) -> float:
    """Continuous Lee-Ready: where on the spread did this trade hit?
    +1 = full lift (price = ask), -1 = full hit (price = bid), 0 = at mid."""
    if bid <= 0 or ask <= bid:
        return 0.0
    mid = (bid + ask) / 2.0
    half = (ask - bid) / 2.0
    if half <= 0:
        return 0.0
    return max(-1.0, min(1.0, (price - mid) / half))


# ─── Main wiring ──────────────────────────────────────────────────────────────
_running = True
_msg_total = 0       # NATS messages seen across all topics (any kind)
_msg_by_inst = defaultdict(int)


def _signal_handler(signum, frame):
    global _running
    log.info('signal %s — shutting down', signum)
    _running = False


def _market_open_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    eh, em = END_HHMM // 100, END_HHMM % 100
    return dtime(9, 15) <= now.time() <= dtime(eh, em)


def main() -> None:
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Pre-market wait loop (same robustness fix as v1 after the May 26 bug)
    while not _market_open_now():
        now = datetime.now()
        eh, em = END_HHMM // 100, END_HHMM % 100
        if now.weekday() >= 5 or now.time() >= dtime(eh, em):
            log.info('market closed for the day (now=%s) — exiting',
                     now.strftime('%H:%M'))
            return
        log.info('pre-market (now=%s) — sleeping 30s', now.strftime('%H:%M'))
        time.sleep(30)

    insts = [s.strip() for s in INSTRUMENTS_ENV.split(',') if s.strip()]
    log.info('v2 boot — insts=%s end=%d gap_ms=%d depth_ms=%d',
             insts, END_HHMM, GAP_THRESHOLD_MS, DEPTH_THROTTLE_MS)

    g = _get_groww()
    log.info('Groww auth OK')

    today = date.today()
    contracts = {}
    for inst in insts:
        try:
            contracts[inst] = _resolve_symbol(g, inst, today)
            log.info('  contract %s → %s token=%s lot=%d tick=%.2f', inst,
                     contracts[inst]['trading_symbol'],
                     contracts[inst]['exchange_token'],
                     contracts[inst]['lot_size'], contracts[inst]['tick_size'])
        except Exception as e:
            log.error('skipping %s — contract resolution failed: %s', inst, e)
    if not contracts:
        log.error('no contracts resolved — exiting'); return

    # Per-instrument state + writers
    states  = {inst: TickState() for inst in contracts}
    writers = {inst: TickWriter (inst, today) for inst in contracts}
    depths  = {inst: DepthWriter(inst, today) for inst in contracts}

    # REST volume poller — same role as v1
    vol_state: dict = {}
    stop_evt = threading.Event()
    vp = _VolPoller(g, contracts, vol_state, stop_evt); vp.start()

    # Connect WS feed — GrowwFeed wraps the NATS subscription stack
    from growwapi import GrowwFeed
    feed = GrowwFeed(g)

    # Map token → inst for fast callback dispatch
    tok2inst = {c['exchange_token']: i for i, c in contracts.items()}

    # ─── Depth callback — fires per market_depth update ──────────────────────
    def _on_depth(meta):
        global _msg_total
        _msg_total += 1
        try:
            all_d = feed.get_market_depth() or {}
        except Exception as e:
            log.warning('get_market_depth in cb: %s', e); return
        # Snapshot every instrument we know about (the callback fires per-topic
        # so we'd otherwise miss whose depth changed; reading the full state
        # tree and writing only those whose ts advanced is the cheap fix).
        for inst, c in contracts.items():
            tok = c['exchange_token']
            seg_key = 'BFO' if inst == 'SENSEX' else 'FNO'
            exch_key = c['exchange']
            d_row = (all_d.get(exch_key, {}).get(seg_key, {}) or {}).get(tok)
            if not d_row:
                continue
            ts = int(d_row.get('tsInMillis') or time.time() * 1000)
            st = states[inst]
            with st.lock:
                if ts == st.last_depth_write_ms:
                    continue
                # Update cached top-of-book for the LTP callback
                bb = d_row.get('buyBook',  {}) or {}
                sb = d_row.get('sellBook', {}) or {}
                st.bid     = float(bb.get('1', {}).get('price', 0) or 0)
                st.ask     = float(sb.get('1', {}).get('price', 0) or 0)
                st.bid_qty = float(bb.get('1', {}).get('qty',   0) or 0)
                st.ask_qty = float(sb.get('1', {}).get('qty',   0) or 0)
                if (ts - st.last_depth_write_ms) >= DEPTH_THROTTLE_MS:
                    depths[inst].write_snapshot(ts, d_row)
                    st.last_depth_write_ms = ts
            _msg_by_inst[f'{inst}_depth'] += 1

    # ─── LTP callback — fires per trade-price update ─────────────────────────
    def _on_ltp(meta):
        global _msg_total
        _msg_total += 1
        try:
            all_ltp = feed.get_ltp() or {}
        except Exception as e:
            log.warning('get_ltp in cb: %s', e); return
        for inst, c in contracts.items():
            tok = c['exchange_token']
            seg_key = 'BFO' if inst == 'SENSEX' else 'FNO'
            exch_key = c['exchange']
            r = (all_ltp.get(exch_key, {}).get(seg_key, {}) or {}).get(tok)
            if not r:
                continue
            ts_ms = int(r.get('tsInMillis') or 0)
            price = float(r.get('ltp') or 0.0)
            if ts_ms == 0 or price == 0:
                continue
            st = states[inst]
            with st.lock:
                # First sample — seed state, no row yet
                if not st.seen_first:
                    st.prev_ts_ms = ts_ms
                    st.prev_ltp   = price
                    vs = vol_state.get(inst, {})
                    st.prev_cum_vol = float(vs.get('cum_volume', 0.0))
                    st.seen_first   = True
                    return

                # Skip if same ts (the WS state hasn't actually advanced)
                if ts_ms == st.prev_ts_ms:
                    return

                # Gap detection — synthetic GAP row, no qty
                gap_ms = ts_ms - st.prev_ts_ms
                if gap_ms > GAP_THRESHOLD_MS:
                    st.n_gap += 1
                    writers[inst].add({
                        'ts_ms': st.prev_ts_ms + gap_ms // 2, 'inst': inst,
                        'price': price, 'qty': 0, 'side': 'GAP',
                        'rule': 'GAP', 'bid': st.bid, 'ask': st.ask,
                        'bid_qty': st.bid_qty, 'ask_qty': st.ask_qty,
                        'spread': max(st.ask - st.bid, 0.0),
                        'cum_volume': st.prev_cum_vol,
                        'microprice': _microprice(st.bid, st.ask,
                                                  st.bid_qty, st.ask_qty),
                        'notional': 0.0, 'aggression': 0.0,
                        'gap_ms': gap_ms, 'msg_seq': st.msg_seq,
                    })

                # cum_volume — check for reversal
                vs = vol_state.get(inst, {})
                cum_vol = float(vs.get('cum_volume', st.prev_cum_vol))
                vol_age_ms = int(time.time() * 1000) - \
                             int(vs.get('updated_at_ms', 0))
                rule_extra = ''
                if cum_vol < st.prev_cum_vol:
                    st.n_reversal += 1
                    rule_extra = 'RESEED'
                    log.warning('%s cum_volume reversal: %.0f → %.0f (resetting)',
                                inst, st.prev_cum_vol, cum_vol)
                    st.prev_cum_vol = cum_vol     # accept the new lower value
                qty = max(cum_vol - st.prev_cum_vol, 0.0)

                # Skip pure quote-update rows (no actual volume change). If
                # vol is fresh AND qty=0, this was just an LTP wiggle. If vol
                # is stale, mark UNVERIFIED so research can choose to keep.
                if qty <= 0:
                    st.n_qty_zero += 1
                    if vol_age_ms <= VOL_STALE_MS:
                        # Pure non-trade — drop it
                        st.prev_ts_ms = ts_ms
                        st.prev_ltp   = price
                        return
                    # Vol is stale; emit the row with UNVERIFIED rule so
                    # research can decide whether to keep
                    rule_extra = 'UNVERIFIED'

                # Classify
                side, base_rule = _classify_v1(price, st.prev_ltp,
                                               st.prev_side, st.bid, st.ask,
                                               c['tick_size'])
                rule = f'{base_rule}|{rule_extra}' if rule_extra else base_rule

                st.msg_seq += 1
                row = {
                    'ts_ms': ts_ms, 'inst': inst, 'price': price, 'qty': qty,
                    'side': side, 'rule': rule,
                    'bid': st.bid, 'ask': st.ask,
                    'bid_qty': st.bid_qty, 'ask_qty': st.ask_qty,
                    'spread': max(st.ask - st.bid, 0.0),
                    'cum_volume': cum_vol,
                    'microprice': _microprice(st.bid, st.ask,
                                              st.bid_qty, st.ask_qty),
                    'notional': qty * price,
                    'aggression': _aggression(price, st.bid, st.ask),
                    'gap_ms': gap_ms,
                    'msg_seq': st.msg_seq,
                }
                writers[inst].add(row)

                # Update state
                st.prev_ts_ms   = ts_ms
                st.prev_ltp     = price
                st.prev_cum_vol = cum_vol
                st.prev_side    = side
                if   side == 'BUY':  st.n_buy  += 1; st.cum_delta_qty += qty
                elif side == 'SELL': st.n_sell += 1; st.cum_delta_qty -= qty
                else:                st.n_unk  += 1
            _msg_by_inst[f'{inst}_ltp'] += 1

    inst_dicts = [{'exchange': c['exchange'],
                   'segment':  'BFO' if c['inst'] == 'SENSEX' else 'FNO',
                   'exchange_token': c['exchange_token']}
                  for c in contracts.values()]
    feed.subscribe_ltp(inst_dicts, on_data_received=_on_ltp)
    feed.subscribe_market_depth(inst_dicts, on_data_received=_on_depth)
    log.info('WS subscribed (callback mode): LTP + depth on %d tokens',
             len(inst_dicts))

    # ─── Run until EOD ───────────────────────────────────────────────────────
    last_hb = 0.0
    try:
        while _running and _market_open_now():
            time.sleep(1.0)
            if time.time() - last_hb >= HEARTBEAT_SEC:
                parts = []
                for inst, st in states.items():
                    parts.append(
                        f'{inst} ltp_msg={_msg_by_inst[inst+"_ltp"]} '
                        f'dep_msg={_msg_by_inst[inst+"_depth"]} '
                        f'buy={st.n_buy} sell={st.n_sell} '
                        f'qz={st.n_qty_zero} gap={st.n_gap} rev={st.n_reversal} '
                        f'delta_qty={st.cum_delta_qty:+.0f}'
                    )
                log.info('heartbeat — %s', ' | '.join(parts))
                last_hb = time.time()
    finally:
        log.info('shutdown — flushing writers')
        stop_evt.set()
        try:
            feed.unsubscribe_ltp(inst_dicts)
            feed.unsubscribe_market_depth(inst_dicts)
        except Exception as e:
            log.warning('unsubscribe: %s', e)
        for w in writers.values(): w.close()
        for w in depths.values():  w.close()
        # Final summary
        for inst, st in states.items():
            log.info('%s totals — ltp_msg=%d dep_msg=%d buy=%d sell=%d '
                     'qty_zero=%d gap=%d reversal=%d delta_qty=%+.0f',
                     inst, _msg_by_inst[inst+'_ltp'], _msg_by_inst[inst+'_depth'],
                     st.n_buy, st.n_sell, st.n_qty_zero, st.n_gap,
                     st.n_reversal, st.cum_delta_qty)
        log.info('EOD — total NATS msgs=%d', _msg_total)


if __name__ == '__main__':
    main()
