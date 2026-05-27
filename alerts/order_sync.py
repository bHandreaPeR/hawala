"""alerts/order_sync.py — Daily Groww order-history fetcher.

Pulls every order placed on the user's Groww account today via
`get_order_list()`, archives the raw payload, and writes two files used
by research + the trade post-mortem flow:

  trade_logs/manual_trades_<YYYY-MM-DD>.csv
      One row per EXECUTED order (raw fills, untouched).

  trade_logs/manual_trades_journal.csv
      Append-only journal of FIFO-matched trade pairs with realised P&L.
      One row per buy→sell pairing per symbol per day. Mirrors the shape
      of vp_paper_journal.csv so the same downstream tooling works.

  v3/cache/groww_orders_<YYYY-MM-DD>.json
      Raw API payload (for forensic re-runs / future re-parsing).

Today's run (post-mortem): would have caught the 11:22 76100CE bet
automatically — same data pulled manually then.

Why FIFO matching: Groww doesn't directly report trade-pair P&L for
multi-leg manual sessions. FIFO is the standard convention and matches
how the user thinks about position sizing (BUY 400 then SELL 200 ×2).

Run:  python -m alerts.order_sync              # today
      python -m alerts.order_sync --date 2026-05-27
      python -m alerts.order_sync --since 2026-05-25
Cron: 5 17 * * 1-5 python -m alerts.order_sync
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
from collections import defaultdict, deque
from datetime import date, datetime

import pyotp

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / 'v3' / 'cache'
TRADE_LOG_DIR = ROOT / 'trade_logs'
TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT / 'logs' / 'trade_bot'
LOG_DIR.mkdir(parents=True, exist_ok=True)

JOURNAL_PATH = TRADE_LOG_DIR / 'manual_trades_journal.csv'
JOURNAL_COLS = [
    'journaled_at', 'trade_date', 'symbol', 'buy_time', 'sell_time',
    'qty', 'buy_price', 'sell_price', 'pnl_rs', 'pnl_pct',
    'win', 'segment', 'product', 'side',  # buy_first ('LONG') or sell_first ('SHORT')
]


# ─── Logging ─────────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    log = logging.getLogger('order_sync')
    log.setLevel(logging.INFO); log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = logging.FileHandler(LOG_DIR / 'order_sync.log', mode='a')
    fh.setFormatter(fmt); log.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Auth ────────────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    env = {}
    for ln in open(ROOT / 'token.env'):
        if '=' in ln:
            k, _, v = ln.strip().partition('=')
            env[k] = v
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    tok  = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
    return GrowwAPI(token=tok)


# ─── Fetch ───────────────────────────────────────────────────────────────────
def fetch_all_orders(g) -> list[dict]:
    """Walk Groww's order list across segments + pages."""
    all_orders = []
    for seg in ('CASH', 'FNO'):
        page = 0
        while True:
            try:
                r = g.get_order_list(segment=seg, page=page, page_size=100)
            except Exception as e:
                log.warning('get_order_list seg=%s page=%d err: %s', seg, page, e)
                break
            orders = r.get('order_list', []) if isinstance(r, dict) else r
            if not orders:
                break
            for o in orders:
                o['_seg'] = seg
                all_orders.append(o)
            if len(orders) < 100:
                break
            page += 1
    return all_orders


def filter_by_date(orders: list[dict], target: date) -> list[dict]:
    target_iso = target.isoformat()
    return [o for o in orders
            if (o.get('created_at') or o.get('order_creation_time') or '').startswith(target_iso)]


# ─── FIFO pairing ────────────────────────────────────────────────────────────
def fifo_pair_pnl(orders: list[dict]) -> list[dict]:
    """Walk orders chronologically; pair opposite-side fills FIFO per symbol.
    Supports both LONG-first (BUY then SELL) and SHORT-first (SELL then BUY).
    Returns one row per closed pair."""
    orders = sorted([o for o in orders if o.get('order_status') == 'EXECUTED'],
                    key=lambda o: o['exchange_time'])
    longs:  dict = defaultdict(deque)  # sym → list of unmatched BUYs
    shorts: dict = defaultdict(deque)  # sym → list of unmatched SELLs
    pairs: list[dict] = []
    journal_at = datetime.now().isoformat(timespec='seconds')

    for o in orders:
        sym  = o['trading_symbol']
        qty  = int(o['filled_quantity'])
        px   = float(o['average_fill_price'])
        side = o['transaction_type']           # 'BUY' or 'SELL'
        ts   = o['exchange_time']
        seg  = o.get('segment', o.get('_seg', ''))
        prod = o.get('product', '')

        if side == 'BUY':
            # First try to close any open SHORT positions
            remaining = qty
            while remaining > 0 and shorts[sym]:
                s = shorts[sym][0]
                taken = min(s[0], remaining)
                pnl   = (s[1] - px) * taken           # sell-then-buy
                pct   = (s[1] - px) / s[1] * 100 if s[1] else 0
                pairs.append({
                    'journaled_at': journal_at,
                    'trade_date':   ts[:10],
                    'symbol':       sym,
                    'buy_time':     ts,
                    'sell_time':    s[2],
                    'qty':          taken,
                    'buy_price':    px,
                    'sell_price':   s[1],
                    'pnl_rs':       round(pnl, 2),
                    'pnl_pct':      round(pct, 3),
                    'win':          1 if pnl > 0 else 0,
                    'segment':      seg,
                    'product':      prod,
                    'side':         'SHORT',
                })
                s[0] -= taken; remaining -= taken
                if s[0] == 0: shorts[sym].popleft()
            if remaining > 0:
                longs[sym].append([remaining, px, ts])
        elif side == 'SELL':
            remaining = qty
            while remaining > 0 and longs[sym]:
                l = longs[sym][0]
                taken = min(l[0], remaining)
                pnl   = (px - l[1]) * taken           # buy-then-sell
                pct   = (px - l[1]) / l[1] * 100 if l[1] else 0
                pairs.append({
                    'journaled_at': journal_at,
                    'trade_date':   ts[:10],
                    'symbol':       sym,
                    'buy_time':     l[2],
                    'sell_time':    ts,
                    'qty':          taken,
                    'buy_price':    l[1],
                    'sell_price':   px,
                    'pnl_rs':       round(pnl, 2),
                    'pnl_pct':      round(pct, 3),
                    'win':          1 if pnl > 0 else 0,
                    'segment':      seg,
                    'product':      prod,
                    'side':         'LONG',
                })
                l[0] -= taken; remaining -= taken
                if l[0] == 0: longs[sym].popleft()
            if remaining > 0:
                shorts[sym].append([remaining, px, ts])

    return pairs


# ─── Writers ─────────────────────────────────────────────────────────────────
def write_raw_orders_csv(orders: list[dict], target: date) -> pathlib.Path:
    """One row per EXECUTED order. Untouched fills, no pairing."""
    p = TRADE_LOG_DIR / f'manual_trades_{target.isoformat()}.csv'
    cols = ['exchange_time', 'created_at', 'trading_symbol', 'transaction_type',
            'order_type', 'order_status', 'quantity', 'filled_quantity',
            'price', 'average_fill_price', 'exchange', 'segment', 'product',
            'groww_order_id']
    with open(p, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for o in sorted(orders, key=lambda x: x['exchange_time']):
            w.writerow(o)
    return p


def write_raw_payload_json(orders: list[dict], target: date) -> pathlib.Path:
    p = CACHE_DIR / f'groww_orders_{target.isoformat()}.json'
    with open(p, 'w') as f:
        json.dump(orders, f, indent=2, default=str)
    return p


def append_journal(pairs: list[dict], target: date) -> int:
    """Append new pairs to manual_trades_journal.csv. Idempotent — if a
    (trade_date, groww-order-style symbol+buy_time+sell_time) row already
    exists, skip it. Returns count of newly-appended rows."""
    if not pairs:
        return 0
    new_pairs = pairs
    if JOURNAL_PATH.exists():
        existing_keys = set()
        with open(JOURNAL_PATH) as f:
            r = csv.DictReader(f)
            for row in r:
                existing_keys.add((row.get('trade_date'), row.get('symbol'),
                                   row.get('buy_time'), row.get('sell_time')))
        new_pairs = [p for p in pairs
                     if (p['trade_date'], p['symbol'], p['buy_time'],
                         p['sell_time']) not in existing_keys]
    if not new_pairs:
        return 0
    new_file = not JOURNAL_PATH.exists()
    with open(JOURNAL_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLS, extrasaction='ignore')
        if new_file: w.writeheader()
        for p in new_pairs: w.writerow(p)
    return len(new_pairs)


# ─── Entrypoint ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat(),
                    help='YYYY-MM-DD (default: today)')
    args = ap.parse_args()
    target = date.fromisoformat(args.date)

    log.info('order_sync — target_date=%s', target)
    g = _get_groww()
    log.info('Groww auth OK')

    all_orders = fetch_all_orders(g)
    log.info('fetched %d total orders', len(all_orders))

    todays = filter_by_date(all_orders, target)
    log.info('  %d orders for %s', len(todays), target)

    if not todays:
        log.info('no orders on %s — nothing to journal', target)
        return

    raw_p   = write_raw_payload_json(todays, target)
    rows_p  = write_raw_orders_csv(todays, target)
    log.info('wrote raw payload   → %s', raw_p)
    log.info('wrote per-order CSV → %s', rows_p)

    pairs = fifo_pair_pnl(todays)
    n_new = append_journal(pairs, target)
    log.info('matched %d pairs, %d newly appended to journal',
             len(pairs), n_new)

    if pairs:
        wins = sum(1 for p in pairs if p['win'])
        gross = sum(p['pnl_rs'] for p in pairs)
        log.info('day summary: %d pairs / %d wins / gross P&L ₹%+.0f',
                 len(pairs), wins, gross)
        for p in sorted(pairs, key=lambda x: x['buy_time']):
            log.info('  %s %s %d @ %.2f→%.2f  P&L ₹%+.0f  %s',
                     p['symbol'], p['side'], p['qty'],
                     p['buy_price'], p['sell_price'], p['pnl_rs'],
                     'WIN' if p['win'] else 'LOSS')


if __name__ == '__main__':
    main()
