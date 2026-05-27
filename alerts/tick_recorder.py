"""alerts/tick_recorder.py — Lee-Ready tick recorder for NIFTY / BANKNIFTY fut.

Phase 1 of footprint pipeline. Subscribes to Groww WebSocket for LTP + 5-level
market depth on NIFTY-FUT + BANKNIFTY-FUT (current month). Each LTP update is
classified using Lee-Ready (quote rule first, tick rule fallback, zero-tick
inherit) and appended to a daily CSV file (mid-day-readable; line-buffered):

    v3/cache/ticks_<INST>_<YYYYMMDD>.csv

Schema (one row per detected print):
    ts_ms            int64    — exchange ts in millis
    inst             str      — 'NIFTY' / 'BANKNIFTY'
    price            float    — LTP at this tick
    qty              float    — print qty (cum_vol delta since previous tick)
    side             str      — 'BUY' / 'SELL' / 'UNK'
    rule             str      — 'QUOTE' / 'TICK' / 'INHERIT'
    bid              float    — best bid at this tick
    ask              float    — best ask at this tick
    bid_qty          float    — best-bid size
    ask_qty          float    — best-ask size
    spread           float    — ask − bid
    cum_volume       float    — cumulative day volume (sanity)

NO trading impact. Read-only observer. Run alongside existing v3 runners.

Env:
    TICK_REC_INSTRUMENTS   csv of NIFTY,BANKNIFTY (default: both)
    TICK_REC_POLL_MS       poll interval ms (default: 100 — 10 Hz)
    TICK_REC_FLUSH_N       flush CSV every N ticks (default: 200)
    TICK_REC_FLUSH_SEC     OR flush every N seconds (default: 30)
    TICK_REC_HEARTBEAT_SEC heartbeat log interval (default: 60)
    TICK_REC_END_HHMM      EOD square-off (default: 1535 — 15:35 IST)

Run:  python -m alerts.tick_recorder
Cron: 9 9 * * 1-5  cd ... && nohup /opt/anaconda3/bin/python3 -m alerts.tick_recorder ...
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
import pickle
import signal
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import csv

import pyotp
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ─── Config (env-overridable) ────────────────────────────────────────────────
INSTRUMENTS_ENV = os.environ.get('TICK_REC_INSTRUMENTS', 'NIFTY,BANKNIFTY')
POLL_MS         = int(os.environ.get('TICK_REC_POLL_MS', '100'))
FLUSH_N         = int(os.environ.get('TICK_REC_FLUSH_N',   '20'))
FLUSH_SEC       = float(os.environ.get('TICK_REC_FLUSH_SEC', '1'))
HEARTBEAT_SEC   = float(os.environ.get('TICK_REC_HEARTBEAT_SEC', '60'))
END_HHMM        = int(os.environ.get('TICK_REC_END_HHMM', '1535'))
DEPTH_SEC       = float(os.environ.get('TICK_REC_DEPTH_SEC', '1.0'))

CACHE_DIR = ROOT / 'v3' / 'cache'
LOG_DIR   = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging (flushing handler — same pattern as option_flow_daemon) ─────────
class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


def _setup_logging() -> logging.Logger:
    log = logging.getLogger('tick_recorder')
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    logpath = LOG_DIR / 'tick_recorder.log'
    fh = _FlushingFileHandler(logpath, mode='a')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Auth (same retry pattern as option_flow_daemon) ─────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    last_err = None
    for attempt in range(3):
        try:
            env = {}
            with open(ROOT / 'token.env') as f:
                for line in f:
                    if '=' in line:
                        k, _, v = line.strip().partition('=')
                        env[k] = v
            totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
            token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(token=token)
        except Exception as e:
            last_err = e
            log.warning("Groww auth attempt %d failed: %s — retrying in 5s",
                        attempt + 1, e)
            time.sleep(5)
    raise RuntimeError(f"Groww auth failed after 3 attempts: {last_err}")


# ─── Contract resolver — current monthly fut for NIFTY / BANKNIFTY ───────────
def _last_tuesday(y: int, m: int) -> date:
    last_day = (date(y + 1, 1, 1) - timedelta(days=1)) if m == 12 \
        else (date(y, m + 1, 1) - timedelta(days=1))
    return last_day - timedelta(days=(last_day.weekday() - 1) % 7)


def _near_monthly_expiry(today: date) -> date:
    OVERRIDES = {date(2026, 3, 31): date(2026, 3, 30)}
    y, m = today.year, today.month
    for _ in range(3):
        exp = OVERRIDES.get(_last_tuesday(y, m), _last_tuesday(y, m))
        if exp >= today:
            return exp
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    raise RuntimeError(f"no monthly expiry found for {today}")


def _resolve_symbol(g, inst: str, today: date) -> dict:
    exp = _near_monthly_expiry(today)
    sym = f"NSE-{inst}-{exp.day}{exp.strftime('%b')}{exp.strftime('%y')}-FUT"
    meta = g.get_instrument_by_groww_symbol(sym)
    return {'inst': inst, 'symbol': sym,
            'exchange_token': str(meta['exchange_token']),
            'trading_symbol': str(meta['trading_symbol']),
            'lot_size': int(meta['lot_size']), 'tick_size': float(meta['tick_size'])}


# ─── REST cum-volume poller (background thread) ──────────────────────────────
# Groww WS LTP stream reports `volume: 0` for FNO derivatives (SDK doesn't
# decode the LIVE_DATA_DETAILED topic). We poll REST get_quote() every
# VOL_POLL_SEC and stash cum-volume + last-trade-qty so the main tick loop can
# attach a real qty to each classified print.
VOL_POLL_SEC     = float(os.environ.get('TICK_REC_VOL_POLL_SEC', '2.0'))
GAP_THRESHOLD_MS = int  (os.environ.get('TICK_REC_GAP_MS',      '5000'))
VOL_STALE_MS     = int  (os.environ.get('TICK_REC_VOL_STALE_MS','3000'))


class _VolPoller(threading.Thread):
    def __init__(self, g, contracts: dict, state: dict, stop_evt: threading.Event):
        super().__init__(daemon=True, name='vol-poller')
        self._g, self._contracts = g, contracts
        self._state, self._stop = state, stop_evt

    def run(self):
        while not self._stop.is_set():
            t0 = time.time()
            for inst, c in self._contracts.items():
                try:
                    r = self._g.get_quote(exchange='NSE', segment='FNO',
                                          trading_symbol=c['trading_symbol'])
                    self._state[inst] = {
                        'cum_volume': float(r.get('volume')   or 0.0),
                        'last_qty':   float(r.get('last_trade_quantity') or 0.0),
                        'last_ts':    int(r.get('last_trade_time')       or 0),
                        'oi':         float(r.get('open_interest') or 0.0),
                        'total_buy_qty':  float(r.get('total_buy_quantity')  or 0.0),
                        'total_sell_qty': float(r.get('total_sell_quantity') or 0.0),
                        'updated_at':    time.time(),
                        'updated_at_ms': int(time.time() * 1000),
                    }
                except Exception as e:
                    log.warning("vol_poll %s err: %s", inst, e)
            dt = time.time() - t0
            if dt < VOL_POLL_SEC:
                self._stop.wait(VOL_POLL_SEC - dt)


# ─── Per-instrument tick classifier (Lee-Ready) ──────────────────────────────
@dataclass
class TickState:
    prev_ts_ms:      int     = 0
    prev_ltp:        float   = 0.0
    prev_cum_vol:    float   = 0.0
    prev_side:       str     = 'UNK'
    seen_first:      bool    = False
    n_buy:           int     = 0
    n_sell:          int     = 0
    n_unk:           int     = 0
    n_qty_zero:      int     = 0
    n_reversal:      int     = 0
    n_gap:           int     = 0
    msg_seq:         int     = 0
    cum_delta_qty:   float   = 0.0
    last_quote_rule_pct: float = 0.0


# ─── Derived feature helpers (microprice + Lee-Ready aggression score) ──────
def _microprice(bid: float, ask: float, bq: float, aq: float) -> float:
    """Size-weighted mid: (ask*bid_qty + bid*ask_qty) / (bid_qty + ask_qty).
    Better than (bid+ask)/2 because it leans toward the thinner side, which
    is where price is most likely to move next."""
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


def classify(price: float, prev_price: float, prev_side: str,
             best_bid: float, best_ask: float,
             tick_size: float) -> tuple[str, str]:
    """Lee-Ready: quote rule first (with ½-tick tolerance), tick rule fallback,
    zero-tick inherit. Returns (side, rule)."""
    eps = max(0.5 * tick_size, 0.025)
    # Quote rule — only valid if quote looks sane
    if best_bid > 0 and best_ask > best_bid and (best_ask - best_bid) < 0.05 * price:
        if price >= best_ask - eps:
            return 'BUY', 'QUOTE'
        if price <= best_bid + eps:
            return 'SELL', 'QUOTE'
    # Tick rule
    if price > prev_price:
        return 'BUY', 'TICK'
    if price < prev_price:
        return 'SELL', 'TICK'
    # Zero-tick inherit
    return prev_side if prev_side in ('BUY', 'SELL') else 'UNK', 'INHERIT'


# ─── Depth-snapshot writer (resting bids/asks, top-5 levels, 1 Hz) ───────────
# Separate file from ticks. Each row = one 5-level snapshot per instrument.
# Schema: ts_ms, inst, level (1..5), side (BID/ASK), price, qty
# At 1 Hz × 6.5h × 2 instruments × 10 rows/snapshot ≈ 470k rows/day ≈ 35 MB CSV.
class DepthWriter:
    COLUMNS = ['ts_ms', 'inst', 'level', 'side', 'price', 'qty']

    def __init__(self, inst: str, run_date: date):
        self.inst = inst
        self.path = CACHE_DIR / f'depth_{inst}_{run_date.strftime("%Y%m%d")}.csv'
        self._fh = open(self.path, 'a', newline='', buffering=1)
        self._w  = csv.DictWriter(self._fh, fieldnames=self.COLUMNS,
                                  extrasaction='ignore')
        if self.path.stat().st_size == 0:
            self._w.writeheader(); self._fh.flush()

    def write_snapshot(self, ts_ms: int, depth_row: dict) -> None:
        if not depth_row:
            return
        rows = []
        for side_key, side_label in (('buyBook', 'BID'), ('sellBook', 'ASK')):
            book = depth_row.get(side_key, {}) or {}
            for lvl_str, item in book.items():
                try:
                    lvl = int(lvl_str)
                except ValueError:
                    continue
                p = float(item.get('price', 0) or 0)
                q = float(item.get('qty',   0) or 0)
                if p <= 0:
                    continue
                rows.append({'ts_ms': ts_ms, 'inst': self.inst, 'level': lvl,
                             'side': side_label, 'price': p, 'qty': q})
        if rows:
            self._w.writerows(rows)

    def close(self) -> None:
        try: self._fh.close()
        except Exception: pass


# ─── CSV writer (append-safe, mid-day readable) ──────────────────────────────
# Earlier prototype used Parquet — but pyarrow's ParquetWriter only finalises
# the footer on close(), so the file is unreadable mid-day. CSV append works.
# ~20k rows/day → ~3 MB/day uncompressed. Acceptable.
class TickWriter:
    """Buffered CSV writer. Flushes when len(buffer) ≥ FLUSH_N or elapsed
    ≥ FLUSH_SEC. One file per instrument per day."""
    # v1.1: added microprice / notional / aggression / gap_ms / msg_seq.
    # Backward-compatible — pd.read_csv on yesterday's files just doesn't see
    # the new columns; today's research already filters by `qty > 0`.
    COLUMNS = [
        'ts_ms', 'inst', 'price', 'qty', 'side', 'rule',
        'bid', 'ask', 'bid_qty', 'ask_qty', 'spread', 'cum_volume',
        'microprice', 'notional', 'aggression', 'gap_ms', 'msg_seq',
    ]

    def __init__(self, inst: str, run_date: date):
        self.inst     = inst
        self.path     = CACHE_DIR / f'ticks_{inst}_{run_date.strftime("%Y%m%d")}.csv'
        self.buffer   = []
        self.last_flush = time.time()
        self._needs_header = not self.path.exists()
        self._fh = open(self.path, 'a', newline='', buffering=1)
        self._w  = csv.DictWriter(self._fh, fieldnames=self.COLUMNS,
                                  extrasaction='ignore')
        if self._needs_header:
            self._w.writeheader()
            self._fh.flush()
            self._needs_header = False

    def add(self, row: dict) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= FLUSH_N or (time.time() - self.last_flush) >= FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        self._w.writerows(self.buffer)
        self._fh.flush()
        self.buffer.clear()
        self.last_flush = time.time()

    def close(self) -> None:
        self.flush()
        try:
            self._fh.close()
        except Exception:
            pass


# ─── Main loop ───────────────────────────────────────────────────────────────
_running = True


def _signal_handler(signum, frame):
    global _running
    log.info("signal %s received — shutting down", signum)
    _running = False


def _best_quote(depth_row: dict) -> tuple[float, float, float, float]:
    """Pull best bid + ask + sizes from a depth payload."""
    if not depth_row:
        return 0.0, 0.0, 0.0, 0.0
    bb = depth_row.get('buyBook',  {}) or {}
    sb = depth_row.get('sellBook', {}) or {}
    bid = bb.get('1', {}).get('price', 0.0) or 0.0
    ask = sb.get('1', {}).get('price', 0.0) or 0.0
    bq  = bb.get('1', {}).get('qty',   0.0) or 0.0
    aq  = sb.get('1', {}).get('qty',   0.0) or 0.0
    return float(bid), float(ask), float(bq), float(aq)


def _market_open_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = dtime(9, 15)
    end_h, end_m = END_HHMM // 100, END_HHMM % 100
    close_t = dtime(end_h, end_m)
    return open_t <= now.time() <= close_t


def main() -> None:
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    insts = [s.strip() for s in INSTRUMENTS_ENV.split(',') if s.strip()]
    log.info("tick_recorder boot — instruments=%s poll=%dms flush_n=%d flush_sec=%.0f end=%d",
             insts, POLL_MS, FLUSH_N, FLUSH_SEC, END_HHMM)

    g = _get_groww()
    today = date.today()
    contracts = {i: _resolve_symbol(g, i, today) for i in insts}
    for i, c in contracts.items():
        log.info("  contract %s → %s token=%s lot=%d tick=%.2f",
                 i, c['symbol'], c['exchange_token'], c['lot_size'], c['tick_size'])

    # Subscribe (one socket, both feeds)
    from growwapi import GrowwFeed
    feed = GrowwFeed(g)
    inst_dicts = [{'exchange': 'NSE', 'segment': 'FNO',
                   'exchange_token': c['exchange_token']}
                  for c in contracts.values()]
    feed.subscribe_ltp(inst_dicts)
    feed.subscribe_market_depth(inst_dicts)
    log.info("WS subscribed: LTP + market_depth on %d tokens", len(inst_dicts))

    states  = {i: TickState() for i in insts}
    writers = {i: TickWriter(i, today) for i in insts}
    depth_writers = {i: DepthWriter(i, today) for i in insts}
    last_depth_write = {i: 0.0 for i in insts}
    vol_state: dict[str, dict] = {}                          # populated by poller
    poll_stop = threading.Event()
    poller = _VolPoller(g, contracts, vol_state, poll_stop)
    poller.start()
    # Give the poller one cycle so vol_state is populated before WS ticks land
    time.sleep(min(VOL_POLL_SEC + 0.5, 3.0))
    poll_dt = POLL_MS / 1000.0
    last_hb = time.time()
    n_ticks_total = 0

    # If cron fired us slightly pre-market (e.g. 09:12), wait — don't exit.
    while _running and not _market_open_now():
        now = datetime.now()
        eh, em = END_HHMM // 100, END_HHMM % 100
        if now.weekday() >= 5 or now.time() >= dtime(eh, em):
            log.info('market closed for the day (now=%s) — exiting',
                     now.strftime('%H:%M'))
            for w in writers.values(): w.close()
            for w in depth_writers.values(): w.close()
            poll_stop.set()
            return
        log.info('pre-market (now=%s) — sleeping 30s', now.strftime('%H:%M'))
        time.sleep(30)

    try:
        while _running and _market_open_now():
            t_loop = time.time()
            try:
                ltp_all   = feed.get_ltp() or {}
                depth_all = feed.get_market_depth() or {}
            except Exception as e:
                log.warning("WS read error: %s — sleeping 2s", e)
                time.sleep(2)
                continue

            for inst, c in contracts.items():
                tok = c['exchange_token']
                ltp_row = (ltp_all.get('NSE', {}).get('FNO', {}) or {}).get(tok)
                dep_row = (depth_all.get('NSE', {}).get('FNO', {}) or {}).get(tok)

                # Persist a depth snapshot at most every DEPTH_SEC
                now_t = time.time()
                if dep_row and (now_t - last_depth_write[inst]) >= DEPTH_SEC:
                    ts_ms_dep = int(dep_row.get('tsInMillis') or now_t * 1000)
                    try:
                        depth_writers[inst].write_snapshot(ts_ms_dep, dep_row)
                    except Exception as e:
                        log.warning("depth write %s: %s", inst, e)
                    last_depth_write[inst] = now_t

                if not ltp_row:
                    continue

                ts_ms      = int(ltp_row.get('tsInMillis') or 0)
                price      = float(ltp_row.get('ltp') or 0.0)
                # WS LTP feed reports volume=0 for FNO — read it from the REST
                # poller's shared state instead. May lag by VOL_POLL_SEC; we
                # still write whatever's freshest.
                vs = vol_state.get(inst, {})
                cum_vol    = float(vs.get('cum_volume', 0.0))
                if ts_ms == 0 or price == 0:
                    continue

                st = states[inst]
                # First snapshot — seed state, no row
                if not st.seen_first:
                    st.prev_ts_ms   = ts_ms
                    st.prev_ltp     = price
                    st.prev_cum_vol = cum_vol
                    st.seen_first   = True
                    continue

                # No new print since last WS tick — skip. (cum_vol comes from
                # the REST poller; using it here would race the poll interval.)
                if ts_ms == st.prev_ts_ms:
                    continue

                bid, ask, bq, aq = _best_quote(dep_row)

                # ── Gap detection ────────────────────────────────────────
                # If we haven't seen a tick for this inst in > GAP_THRESHOLD_MS
                # write a synthetic marker row so research can filter/count
                # gaps without re-deriving from raw ts_ms diffs. Common cause:
                # WS reconnect, app sleep, Mac backgrounded.
                gap_ms = ts_ms - st.prev_ts_ms
                if gap_ms > GAP_THRESHOLD_MS:
                    st.n_gap += 1
                    writers[inst].add({
                        'ts_ms': st.prev_ts_ms + gap_ms // 2, 'inst': inst,
                        'price': price, 'qty': 0, 'side': 'GAP', 'rule': 'GAP',
                        'bid': bid, 'ask': ask, 'bid_qty': bq, 'ask_qty': aq,
                        'spread': max(ask - bid, 0.0),
                        'cum_volume': st.prev_cum_vol,
                        'microprice': _microprice(bid, ask, bq, aq),
                        'notional': 0.0, 'aggression': 0.0,
                        'gap_ms': gap_ms, 'msg_seq': st.msg_seq,
                    })

                # ── cum_volume reversal detection ────────────────────────
                # When the broker returns a SMALLER cum_volume than we last
                # saw (happens during reconnect / API hiccup) we used to
                # silently clamp qty to 0 forever via max(cum_vol - prev, 0).
                # Now: detect, log, re-seed prev to the new lower value, tag
                # the resulting row with RESEED. The bug that wedged today's
                # 10:20-10:28 window was exactly this — never silent again.
                rule_extra = ''
                if cum_vol < st.prev_cum_vol:
                    st.n_reversal += 1
                    rule_extra = 'RESEED'
                    log.warning('%s cum_volume reversal: %.0f → %.0f (re-seeding)',
                                inst, st.prev_cum_vol, cum_vol)
                    st.prev_cum_vol = cum_vol
                qty = max(cum_vol - st.prev_cum_vol, 0.0)

                # ── qty=0 filter ─────────────────────────────────────────
                # qty=0 means "WS pushed a new timestamp but vol-poller hasn't
                # caught up yet". If vol-poller is FRESH (<= VOL_STALE_MS),
                # the gap is a quote-only update — drop it (phantom row that
                # used to inflate tick-count imbalance features in research).
                # If vol-poller is STALE, keep the row but tag UNVERIFIED so
                # research can decide whether to use it.
                vol_age_ms = int(time.time() * 1000) - \
                             int(vs.get('updated_at_ms', 0))
                if qty <= 0:
                    st.n_qty_zero += 1
                    if vol_age_ms <= VOL_STALE_MS:
                        # Pure quote update — advance state, no row written
                        st.prev_ts_ms = ts_ms
                        st.prev_ltp   = price
                        continue
                    rule_extra = (rule_extra + '|UNVERIFIED').lstrip('|') \
                                  if rule_extra else 'UNVERIFIED'

                side, base_rule = classify(price, st.prev_ltp, st.prev_side,
                                           bid, ask, c['tick_size'])
                rule = f'{base_rule}|{rule_extra}' if rule_extra else base_rule

                st.msg_seq += 1
                writers[inst].add({
                    'ts_ms': ts_ms, 'inst': inst, 'price': price, 'qty': qty,
                    'side': side, 'rule': rule,
                    'bid': bid, 'ask': ask, 'bid_qty': bq, 'ask_qty': aq,
                    'spread': max(ask - bid, 0.0), 'cum_volume': cum_vol,
                    'microprice': _microprice(bid, ask, bq, aq),
                    'notional': qty * price,
                    'aggression': _aggression(price, bid, ask),
                    'gap_ms': gap_ms,
                    'msg_seq': st.msg_seq,
                })

                # State + counters
                st.prev_ts_ms   = ts_ms
                st.prev_ltp     = price
                st.prev_cum_vol = cum_vol
                st.prev_side    = side
                if   side == 'BUY':  st.n_buy  += 1; st.cum_delta_qty += qty
                elif side == 'SELL': st.n_sell += 1; st.cum_delta_qty -= qty
                else:                st.n_unk  += 1
                n_ticks_total += 1

            # Heartbeat
            if time.time() - last_hb >= HEARTBEAT_SEC:
                parts = []
                for inst, st in states.items():
                    parts.append(
                        f"{inst} ticks={st.n_buy+st.n_sell+st.n_unk} "
                        f"buy={st.n_buy} sell={st.n_sell} "
                        f"qz={st.n_qty_zero} gap={st.n_gap} rev={st.n_reversal} "
                        f"delta_qty={st.cum_delta_qty:+.0f}"
                    )
                log.info("heartbeat — " + " | ".join(parts))
                last_hb = time.time()

            # Pace
            elapsed = time.time() - t_loop
            if elapsed < poll_dt:
                time.sleep(poll_dt - elapsed)

    except Exception as e:
        log.exception("fatal in tick loop: %s", e)
    finally:
        poll_stop.set()
        for w in writers.values():
            w.close()
        for dw in depth_writers.values():
            dw.close()
        # Try to unsubscribe cleanly (best-effort)
        try:
            feed.unsubscribe_ltp(inst_dicts)
            feed.unsubscribe_market_depth(inst_dicts)
        except Exception:
            pass
        poller.join(timeout=3)
        # Final summary
        for inst, st in states.items():
            tot = st.n_buy + st.n_sell + st.n_unk
            log.info("EOD %s: ticks=%d buy=%d sell=%d unk=%d cum_delta=%+.0f → %s",
                     inst, tot, st.n_buy, st.n_sell, st.n_unk,
                     st.cum_delta_qty,
                     str(writers[inst].path.name))


if __name__ == '__main__':
    main()
