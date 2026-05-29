"""
alerts/option_flow_daemon.py — Real-time strike-level option flow tracker.

This is the retail-Indian-market analog to Bookmap's Cumulative Volume Delta
("CVD"). Every 5 seconds during market hours, it polls the option chain for
NIFTY + BANKNIFTY, classifies each strike's OI flow into one of 8 positioning
states, weights by ATM-proximity × cash-value, and accumulates a signed
session score.

Two outputs:
  1. JSON file per instrument at  v3/cache/option_flow_<inst>.json
       Used as a confluence input by v3 runners (opt-in via env var).
  2. Telegram alerts to the MACRO channel on three trigger types:
       'flip'      — cumulative CVD crosses zero with conviction
       'sustained' — N consecutive same-direction snapshots
       'z_high'    — single-snapshot z-score > 2.0 vs rolling history

Rate limits:
  - 1 alert per instrument per direction per 15 min (cooldown)
  - Heartbeat log every 60s with current state

Modes:
  --mode test     send sample alert and exit
  --mode oneshot  one poll cycle on each instrument, write JSON, exit
  --mode daemon   run until 15:30 IST (default — used by cron at 09:12)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import time
import concurrent.futures
import tempfile
from datetime import datetime, time as dtime, timedelta

import pyotp
import pytz

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alerts.telegram import send as tg_send                          # noqa: E402
from v3.signals.option_flow import (                                  # noqa: E402
    score_snapshot, cumulative_state_update, empty_state,
    z_score, FLOW_DIRECTION,
    update_conviction, conviction_signal, band_name,
)

IST          = pytz.timezone('Asia/Kolkata')
POLL_SECS    = int(os.environ.get("OPTION_FLOW_POLL_SECS", "5"))
HEARTBEAT_SECS = 60
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ALERT_COOLDOWN_SEC = int(os.environ.get("OPTION_FLOW_COOLDOWN_SEC", "900"))   # 15 min

# Alert mode — see _maybe_alert docstring.
#   live    — DEFAULT. Conviction-gated: alerts only on buildup band
#             escalation / direction flip (~2-5 quality alerts/day).
#   summary — no intraday alerts, ONE EOD summary message.
#   off     — no Telegram at all (data-only).
# With the conviction accumulator, 'live' is already low-volume + high-
# quality, so it's the sensible default — the user wants to SEE the buildup.
ALERT_MODE = os.environ.get("OPTION_FLOW_ALERT_MODE", "live").lower()

# Hard timeouts for API calls — these protect against the daemon hanging if
# Groww (or its upstream) stops responding. SDK has its own timeouts but
# they aren't always honoured; this is a belt-and-braces wrapper.
API_TIMEOUT_SEC          = int(os.environ.get("OPTION_FLOW_API_TIMEOUT", "8"))
EXPIRY_API_TIMEOUT_SEC   = int(os.environ.get("OPTION_FLOW_EXP_TIMEOUT", "10"))

# Auth lifetime — Groww tokens typically last ~8h, but we re-auth more
# frequently to survive any unexpected expiry mid-session.
AUTH_REFRESH_SEC         = int(os.environ.get("OPTION_FLOW_AUTH_REFRESH", "3600"))

# Backoff schedule on persistent API failures (consecutive failures, sleep_secs)
# Anything beyond 8 failures sleeps for 60s — caps the polling rate when API
# is genuinely down, avoiding a fail-storm.
BACKOFF_SCHEDULE = [(0, 5), (3, 10), (5, 30), (8, 60)]

# Conviction-accumulator tunables (May-2026 redesign). Poll stays 5s; the
# anomaly gate + EMA conviction decide WHEN a buildup is alert-worthy.
CONV_ALPHA  = float(os.environ.get("OPTION_FLOW_CONV_ALPHA", "0.25"))  # newest-anomaly weight
CONV_DECAY  = float(os.environ.get("OPTION_FLOW_CONV_DECAY", "0.02"))  # quiet-tick decay
CONV_Z_MIN  = float(os.environ.get("OPTION_FLOW_CONV_ZMIN",  "2.5"))   # anomaly z-bar

# Per-minute option OI is persisted to option_oi_1m_<INST>.pkl this often (and
# once more at EOD). 60s keeps the viewer's S&R walls fresh without thrashing
# disk — the in-memory accumulator already holds the full session.
OI_PERSIST_SECS = int(os.environ.get("OPTION_FLOW_OI_PERSIST_SECS", "60"))

CACHE_DIR    = ROOT / 'v3' / 'cache'
LOG_DIR      = ROOT / 'logs' / 'macro_bot'

INSTRUMENTS = {
    'NIFTY':     {'exchange': 'NSE',  'underlying': 'NIFTY',     'lot_size': 75,
                  'strike_step': 50,  'atm_band_strikes': 20},
    'BANKNIFTY': {'exchange': 'NSE',  'underlying': 'BANKNIFTY', 'lot_size': 30,
                  'strike_step': 100, 'atm_band_strikes': 15},
    # SENSEX added 2026-05-29 — BSE option chain (get_option_chain
    # exchange='BSE' underlying='SENSEX' verified working). Lot 20, strikes
    # 100 apart. Per-inst try/except in oneshot isolates any BSE quirk so it
    # can't disturb NIFTY/BANKNIFTY capture.
    'SENSEX':    {'exchange': 'BSE',  'underlying': 'SENSEX',    'lot_size': 20,
                  'strike_step': 100, 'atm_band_strikes': 15},
}

log = logging.getLogger('option_flow_daemon')


# ─────────────────────────────────────────────────────────────────────────────
# Auth (mirrors v3 runner pattern)
# ─────────────────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env_path = ROOT / 'token.env'
    env: dict = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _get_groww():
    from growwapi import GrowwAPI
    env = _load_env()
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    for attempt in range(3):
        try:
            tok = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            return GrowwAPI(tok)
        except Exception as e:
            if attempt < 2:
                log.warning("Groww auth attempt %d failed, retrying: %s", attempt+1, e)
                time.sleep(5)
            else:
                raise


# ── Resilience helpers ───────────────────────────────────────────────────────
# Shared single-thread executor for timeout-wrapped API calls. One executor for
# the daemon's lifetime; we submit each call as a future and cancel on timeout.
_API_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                      thread_name_prefix="grw")


def _call_with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run `fn(*args, **kwargs)` with a hard timeout.

    Raises TimeoutError if the call doesn't return within timeout_s.
    Any exception raised by fn propagates as-is.
    """
    fut = _API_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        # The submitted call is still running on the worker thread. We can't
        # forcibly kill a Python thread; the SDK's own timeout (if any) will
        # eventually return. The next call submits a new future — the executor
        # will queue it after the stuck call returns. Two consecutive timeouts
        # will queue up, but the backoff schedule slows the cadence enough
        # that the executor catches up.
        raise TimeoutError(f"API call exceeded {timeout_s}s")


def _atomic_write_json(path: pathlib.Path, data: dict | list) -> None:
    """Write JSON atomically: tmp file in same dir + rename.

    Prevents corrupted state files if the process dies mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile must be in same filesystem as path for atomic rename
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try:    os.unlink(tmp)
        except: pass
        raise


def _atomic_write_pickle(path: pathlib.Path, obj) -> None:
    """Pickle `obj` to `path` atomically (tmp file in same dir + os.replace).

    The viewer reads option_oi_1m_<INST>.pkl while the daemon writes it every
    minute — a non-atomic dump would expose a truncated file. os.replace is
    atomic on POSIX, so a reader always sees either the old or new file whole."""
    import pickle as _pickle
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            _pickle.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        try:    os.unlink(tmp)
        except: pass
        raise


def _accumulate_oi(inst: str, chain: dict, oi_accum: dict,
                   last_oi_minute: dict) -> None:
    """Append one per-strike OI row per minute from the live option chain.

    The daemon polls every POLL_SECS (~5s); we down-sample to 1 row/minute so
    the on-disk series matches the '1m' cache the viewer/backtests expect. The
    first poll of each new IST minute wins (value at :00s).

    oi_accum[inst][strike] = {'CE': [(ts, close, volume, oi)], 'PE': [...]}

    Only strikes within the configured ATM band are kept (matches the runners'
    ±band and what the viewer plots after its ±5% filter) — the full chain is
    ~150 strikes and the far-OTM tails are noise for S&R walls.
    """
    import pandas as pd
    now_min = pd.Timestamp(datetime.now(IST)).floor('min').tz_localize(None)
    if last_oi_minute.get(inst) == now_min:
        return
    last_oi_minute[inst] = now_min
    cfg  = INSTRUMENTS[inst]
    spot = float(chain.get('underlying_ltp', 0) or 0)
    band = cfg['strike_step'] * cfg['atm_band_strikes']
    acc = oi_accum.setdefault(inst, {})
    for K in chain.get('strikes', []):
        if spot > 0 and abs(K - spot) > band:
            continue
        sides = acc.setdefault(K, {'CE': [], 'PE': []})
        sides['CE'].append((now_min,
                            chain['ce_ltp'].get(K, 0.0),
                            chain['ce_vol'].get(K, 0.0),
                            chain['ce_oi'].get(K, 0.0)))
        sides['PE'].append((now_min,
                            chain['pe_ltp'].get(K, 0.0),
                            chain['pe_vol'].get(K, 0.0),
                            chain['pe_oi'].get(K, 0.0)))


def _persist_option_oi(inst: str, oi_accum: dict) -> None:
    """Write the accumulated per-minute OI for `inst` into option_oi_1m_<INST>.pkl.

    Real-time OI from get_option_chain (open_interest) — the reliable source.
    The historical-candle fetchers (fetch_option_oi_*.py) can't supply OI for
    a still-active weekly contract, so the daemon is the live writer the viewer
    docstring already names.

    Cache format (matches v3/data/fetch_option_oi_*.py and the live runners):
        {date_str: {strike: {'CE': DataFrame[ts, close, volume, oi, oi_raw],
                              'PE': DataFrame[ts, close, volume, oi, oi_raw]}}}

    Merges into today's entry rather than overwriting it wholesale, so strikes
    written by a runner (wider band) survive alongside the daemon's strikes."""
    import pandas as pd
    import pickle as _pickle
    inst_accum = oi_accum.get(inst) or {}
    if not inst_accum:
        return
    path = CACHE_DIR / f'option_oi_1m_{inst}.pkl'
    try:
        cache: dict = {}
        if path.exists():
            with open(path, 'rb') as fh:
                cache = _pickle.load(fh)
        today_str = datetime.now(IST).strftime('%Y-%m-%d')
        existing = cache.get(today_str)
        day_entry: dict = dict(existing) if isinstance(existing, dict) else {}
        cols = ['ts', 'close', 'volume', 'oi', 'oi_raw']
        for strike, sides in inst_accum.items():
            entry: dict = {}
            for side in ('CE', 'PE'):
                rows = sides.get(side, [])
                if not rows:
                    entry[side] = pd.DataFrame(columns=cols)
                    continue
                df = pd.DataFrame(rows, columns=['ts', 'close', 'volume', 'oi'])
                df['ts']     = pd.to_datetime(df['ts'])
                df['oi_raw'] = pd.to_numeric(df['oi'], errors='coerce')
                df['oi']     = df['oi_raw'].ffill().fillna(0)
                entry[side]  = df[cols].copy()
            day_entry[strike] = entry
        cache[today_str] = day_entry
        _atomic_write_pickle(path, cache)
        log.debug("[%s] persist_option_oi: %d strikes → %s",
                  inst, len(day_entry), path)
    except Exception as e:
        log.warning("[%s] persist_option_oi FAILED: %s — data stays in memory",
                    inst, e)


def _safe_read_json(path: pathlib.Path) -> dict | None:
    """Read JSON file, return None on missing/corrupt rather than raising."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state file corrupt or unreadable %s: %s", path, e)
        return None


def _backoff_sleep(consecutive_failures: int) -> int:
    """Map failure-count to poll-interval per BACKOFF_SCHEDULE."""
    interval = POLL_SECS
    for threshold, secs in BACKOFF_SCHEDULE:
        if consecutive_failures >= threshold:
            interval = secs
    return interval


def _is_auth_error(exc: Exception) -> bool:
    """Heuristic: detect Groww auth-expiry errors from message text."""
    msg = str(exc).lower()
    return any(k in msg for k in
               ('unauthor', 'token', 'auth', 'expired', 'forbidden', '401', '403'))


def _telegram_macro() -> tuple[str, list[str]]:
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN_MACRO", "").strip() \
            or env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats_raw = env.get("TELEGRAM_CHAT_IDS_MACRO", "").strip() \
                or env.get("TELEGRAM_CHAT_IDS", "").strip()
    chats = [c.strip() for c in chats_raw.split(",") if c.strip()]
    return token, chats


# ─────────────────────────────────────────────────────────────────────────────
# Expiry resolution
# ─────────────────────────────────────────────────────────────────────────────
def _chain_has_atm_oi(strikes: dict, spot: float) -> bool:
    """True if a strike within ~2% of spot carries nonzero CE/PE OI — i.e. a
    real, ACTIVE expiry (distinguishes the live weekly from an empty date)."""
    if not strikes or spot <= 0:
        return False
    band = 0.02 * spot
    for k, sides in strikes.items():
        try:
            if abs(float(k) - spot) > band:
                continue
        except (TypeError, ValueError):
            continue
        for s in ('CE', 'PE'):
            oi = (sides.get(s) or {}).get('open_interest', 0) or 0
            try:
                if float(oi) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _resolve_weekly_expiry(g, exchange: str, underlying: str) -> str:
    """Nearest TRADEABLE expiry on/after today.

    get_expiries has been observed returning a stale, MONTHLY-only list (e.g.
    nearest = month-end) that MISSES the active weekly the market trades. Since
    get_option_chain accepts any expiry_date directly, we build candidate dates
    (get_expiries results + the next ~10 weekdays) and return the SOONEST whose
    chain is actually populated with near-the-money OI. Falls back to the
    soonest candidate if none probe clean."""
    today = datetime.now(IST).date()
    cands: set[str] = set()
    try:
        r = _call_with_timeout(g.get_expiries, EXPIRY_API_TIMEOUT_SEC,
                               exchange=exchange, underlying_symbol=underlying)
        for e in (r or {}).get('expiries', []) or []:
            if e >= today.isoformat():
                cands.add(e)
    except Exception as e:
        log.warning("get_expiries failed for %s: %s (probing dates instead)", underlying, e)
    # get_expiries can be stale → also probe the next 10 weekdays (expiries are
    # weekdays) so we catch the active weekly it omits.
    for i in range(1, 11):
        d = today + timedelta(days=i)
        if d.weekday() < 5:
            cands.add(d.isoformat())
    for e in sorted(cands):
        try:
            r = _call_with_timeout(g.get_option_chain, EXPIRY_API_TIMEOUT_SEC,
                                   exchange=exchange, underlying=underlying,
                                   expiry_date=e)
        except Exception:
            continue
        if _chain_has_atm_oi((r or {}).get('strikes') or {},
                             float((r or {}).get('underlying_ltp', 0) or 0)):
            log.info("resolved nearest active expiry for %s: %s", underlying, e)
            return e
    fallback = sorted(cands)[0] if cands else today.isoformat()
    log.warning("no populated chain probed for %s — falling back to %s", underlying, fallback)
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Option chain fetch → normalised dict for option_flow.score_snapshot
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_chain(g, inst: str, expiry: str) -> dict | None:
    """Fetch + normalise the option chain. Returns None on any failure
    (timeout, API error, empty response, auth issue). Caller must handle
    None — it signals 'skip this snapshot, try again next cycle'."""
    cfg = INSTRUMENTS[inst]
    try:
        r = _call_with_timeout(
            g.get_option_chain, API_TIMEOUT_SEC,
            exchange=cfg['exchange'],
            underlying=cfg['underlying'],
            expiry_date=expiry,
        )
    except TimeoutError:
        log.warning("[%s] get_option_chain TIMEOUT after %ds", inst, API_TIMEOUT_SEC)
        return None
    except Exception as e:
        # Re-raise auth errors so the main loop can trigger a re-auth.
        if _is_auth_error(e):
            log.warning("[%s] get_option_chain AUTH error: %s", inst, e)
            raise
        log.warning("[%s] get_option_chain failed: %s", inst, e)
        return None
    if not r or not r.get('strikes'):
        return None

    spot = float(r.get('underlying_ltp', 0) or 0)
    raw  = r.get('strikes', {})
    # Groww format: dict[str_strike → {CE: {open_interest, ltp, volume}, PE: {...}}]
    if not isinstance(raw, dict):
        log.warning("[%s] unexpected strikes format: %s", inst, type(raw))
        return None

    strikes_int: list[float] = []
    ce_oi:  dict[float, float] = {}
    pe_oi:  dict[float, float] = {}
    ce_ltp: dict[float, float] = {}
    pe_ltp: dict[float, float] = {}
    ce_vol: dict[float, float] = {}
    pe_vol: dict[float, float] = {}

    for K_s, data in raw.items():
        try:
            K = float(K_s)
        except Exception:
            continue
        ce = (data or {}).get('CE', {}) or {}
        pe = (data or {}).get('PE', {}) or {}
        strikes_int.append(K)
        ce_oi[K]  = float(ce.get('open_interest', 0) or 0)
        pe_oi[K]  = float(pe.get('open_interest', 0) or 0)
        ce_ltp[K] = float(ce.get('ltp', 0) or 0)
        pe_ltp[K] = float(pe.get('ltp', 0) or 0)
        ce_vol[K] = float(ce.get('volume', 0) or 0)
        pe_vol[K] = float(pe.get('volume', 0) or 0)

    strikes_int.sort()
    return {
        'underlying_ltp': spot,
        'strikes':        strikes_int,
        'ce_oi':          ce_oi,
        'pe_oi':          pe_oi,
        'ce_ltp':         ce_ltp,
        'pe_ltp':         pe_ltp,
        'ce_vol':         ce_vol,
        'pe_vol':         pe_vol,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Alert formatting
# ─────────────────────────────────────────────────────────────────────────────
def _format_alert(inst: str, kind: str, snap: dict, state: dict) -> str:
    spot = float(snap.get('spot', 0))
    net  = float(snap.get('net', 0))
    cvd  = float(state.get('cvd', 0))
    conv = float(state.get('conviction', 0.0))
    band = int(state.get('conv_band', 0))
    peak = float(state.get('conv_peak', 0.0))
    n_anom = int(state.get('anomaly_count', 0))
    streak = int(state.get('consec_anom_n', 0))
    bname  = band_name(band)
    direction = '🟢 BULLISH' if conv > 0 else ('🔴 BEARISH' if conv < 0 else '⚪ FLAT')

    kind_emoji = {
        'conviction_escalate': '📊',
        'conviction_flip':     '🔄',
    }.get(kind, '🌊')

    kind_label = {
        'conviction_escalate': f'buildup escalated → {bname}',
        'conviction_flip':     f'direction FLIPPED → {bname}',
    }.get(kind, kind)

    head = (f"{kind_emoji} <b>{inst} Option Flow</b> — {direction} "
            f"<b>{bname}</b>\n"
            f"<i>{datetime.now(IST):%Y-%m-%d %H:%M:%S IST}  spot={spot:,.1f}</i>")

    # The conviction line IS the cumulative-buildup story
    sub = (f"\n<b>{kind_label}</b>"
           f"\nConviction: <b>{conv:+.2f}</b>  (band {band:+d}, peak {peak:+.2f})"
           f"\nBuilt from <b>{n_anom}</b> anomalies · current streak {streak} same-dir"
           f"\nSnapshot net={net:+,.0f}  ·  session CVD={cvd:+,.0f}")

    top_lines = []
    for t in (snap.get('top_strikes') or [])[:4]:
        side = t.get('side', '?')
        K    = int(t.get('strike', 0))
        st   = t.get('state', '?')
        w    = float(t.get('weight', 0))
        top_lines.append(f"  • {K} {side}  {st}  (₹{w:,.0f})")
    top = "\n<b>Top strikes now:</b>\n" + "\n".join(top_lines) if top_lines else ""

    foot = ("\n\n<i>EMA conviction accumulator · anomaly-gated · "
            "fires on buildup state-change, not per tick.</i>")

    return head + sub + top + foot


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────
def _state_path(inst: str) -> pathlib.Path:
    return CACHE_DIR / f'option_flow_{inst}.json'


def _write_state(inst: str, snap: dict, state: dict, last_sig: str | None) -> None:
    """Atomically write the per-instrument state file. Never partial-write."""
    payload = {
        'inst':       inst,
        'ts':         datetime.now(IST).isoformat(),
        'spot':       float(snap.get('spot', 0)),
        'net':        float(snap.get('net', 0)),
        'bull':       float(snap.get('bull', 0)),
        'bear':       float(snap.get('bear', 0)),
        'cvd':        float(state.get('cvd', 0)),
        'sustained':  int(state.get('sustained', 0)),
        'sustained_dir': int(state.get('sustained_dir', 0)),
        'z':          float(z_score(snap, state)),
        'last_signal': last_sig or '',
        'top_strikes': snap.get('top_strikes') or [],
        'state_counts': snap.get('state_counts') or {},
        # ── Conviction accumulator state — the buildup story ──
        'conviction':    round(float(state.get('conviction', 0.0)), 3),
        'conv_band':     int(state.get('conv_band', 0)),
        'conv_peak':     round(float(state.get('conv_peak', 0.0)), 3),
        'anomaly_count': int(state.get('anomaly_count', 0)),
        'consec_anom_n': int(state.get('consec_anom_n', 0)),
        # v3 runners read `direction` for the F6 confluence veto — base it
        # on the CONVICTION sign now (the buildup), not raw tick net.
        'direction':  +1 if float(state.get('conviction', 0)) > 0
                      else (-1 if float(state.get('conviction', 0)) < 0 else 0),
    }
    try:
        _atomic_write_json(_state_path(inst), payload)
    except Exception as e:
        log.warning("[%s] _write_state failed: %s", inst, e)

    # Append-only NDJSON trace for post-session analysis (lead/lag, hit rate).
    # One line per snapshot per instrument. ~9,400 lines/day total for 2 inst.
    # File rolls daily: option_flow_trace_<inst>_<YYYYMMDD>.ndjson
    try:
        date_str = datetime.now(IST).strftime('%Y%m%d')
        trace_path = CACHE_DIR / f'option_flow_trace_{inst}_{date_str}.ndjson'
        with open(trace_path, 'a') as f:
            f.write(json.dumps({
                'ts':     payload['ts'],
                'spot':   payload['spot'],
                'net':    payload['net'],
                'cvd':    payload['cvd'],
                'z':      payload['z'],
                'sustained': payload['sustained'],
                'sustained_dir': payload['sustained_dir'],
                'sig':    last_sig or None,
            }, default=str) + '\n')
    except Exception as e:
        log.warning("[%s] trace append failed: %s", inst, e)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level orchestration
# ─────────────────────────────────────────────────────────────────────────────
class _FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every emit — no in-memory buffering.
    Without this, heartbeat log lines stayed in buffer and never reached
    disk during Friday May 15 6-hour session (only error-path log lines
    flushed). Critical for daemon-alive forensics."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def _send_eod_summary(states: dict, token: str, chats: list[str]) -> None:
    """One MACRO message at market close summarising the day's option flow.
    Replaces per-event spam in 'summary' mode."""
    if not token or not chats:
        return
    lines = [f"🌊 <b>Option Flow — EOD Summary {datetime.now(IST):%d-%b}</b>"]
    for inst, s in states.items():
        conv     = float(s.get('conviction', 0))
        peak     = float(s.get('conv_peak', 0))
        band     = int(s.get('conv_band', 0))
        n_anom   = int(s.get('anomaly_count', 0))
        cvd      = float(s.get('cvd', 0))
        bias     = '🟢 bullish' if conv > 0 else ('🔴 bearish' if conv < 0 else '⚪ flat')
        lines.append(f"\n<b>{inst}</b>: {bias} {band_name(band)}")
        lines.append(f"  conviction close: {conv:+.2f}  ·  peak: {peak:+.2f}")
        lines.append(f"  anomalies today: {n_anom}  ·  session CVD: {cvd:+,.0f}")
    lines.append("\n<i>Daemon polled 5s all day. Conviction = EMA-weighted "
                  "buildup of anomalous flow ticks.</i>")
    msg = "\n".join(lines)
    for c in chats:
        try:
            tg_send(token, c, msg)
        except Exception as e:
            log.warning("EOD summary send failed: %s", e)
    log.info("EOD summary sent")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = _FlushingFileHandler(LOG_DIR / 'option_flow.log', mode='a')
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    for h in (fh, sh):
        h.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.setLevel(logging.INFO)


def _maybe_alert(inst: str, kind: str, snap: dict, state: dict,
                 token: str, chats: list[str]) -> bool:
    """Send alert if cooldown allows. Returns True if sent.

    Alert mode (env OPTION_FLOW_ALERT_MODE):
      summary  — DEFAULT. Suppress per-event Telegram. Daemon still polls
                 + writes trace/state JSON. ONE summary message at market
                 close. The option_flow signal is not yet trade-validated
                 (POC deprioritised) so per-event spam has no value and
                 was overpowering the news feed on the MACRO channel.
      live     — old behaviour: per-event alerts (cooldown-gated)
      off      — no Telegram at all (data-only)
    """
    if ALERT_MODE != 'live':
        # summary/off mode — conviction state (conv_peak, anomaly_count) is
        # already maintained by update_conviction(); nothing to track here.
        # The EOD summary reads it directly off `state`.
        return False
    if not token or not chats:
        return False
    last_ts   = state.get('last_alert_ts')
    last_kind = state.get('last_alert_kind')
    now = datetime.now(IST)
    cur_dir = +1 if float(snap.get('net', 0)) > 0 else -1
    last_dir = state.get('last_alert_dir', 0)

    # Cooldown: only suppress if SAME direction within window.
    if last_ts:
        try:
            elapsed = (now - datetime.fromisoformat(last_ts)).total_seconds()
            if elapsed < ALERT_COOLDOWN_SEC and last_dir == cur_dir:
                return False
        except Exception:
            pass

    text = _format_alert(inst, kind, snap, state)
    sent = False
    for c in chats:
        if tg_send(token, c, text):
            sent = True
    if sent:
        state['last_alert_ts']   = now.isoformat()
        state['last_alert_kind'] = kind
        state['last_alert_dir']  = cur_dir
        # NB: comma grouping (`,`) is NOT valid in %-style format — use f-string.
        log.info(f"[{inst}] ALERT kind={kind} "
                 f"net={float(snap.get('net', 0)):+,.0f} "
                 f"cvd={float(state.get('cvd', 0)):+,.0f} → {len(chats)} chats")
    return sent


def _is_market_now() -> bool:
    t = datetime.now(IST).time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def oneshot(g, instruments: list[str],
            prev_snaps: dict, states: dict, expiries: dict,
            token: str, chats: list[str],
            oi_accum: dict | None = None,
            last_oi_minute: dict | None = None) -> None:
    """Single poll cycle across all instruments."""
    for inst in instruments:
        if inst not in expiries:
            expiries[inst] = _resolve_weekly_expiry(g, INSTRUMENTS[inst]['exchange'],
                                                    INSTRUMENTS[inst]['underlying'])
        chain = _fetch_chain(g, inst, expiries[inst])
        if chain is None:
            continue

        # Capture per-minute OI before scoring — the seed-snapshot path below
        # returns early, but the first minute's OI must still be recorded.
        if oi_accum is not None and last_oi_minute is not None:
            _accumulate_oi(inst, chain, oi_accum, last_oi_minute)

        prev = prev_snaps.get(inst)
        if prev is None:
            # First snapshot — seed only, no flow to compute yet
            prev_snaps[inst] = chain
            log.info("[%s] seed snapshot at spot=%.1f, strikes=%d, expiry=%s",
                     inst, chain['underlying_ltp'], len(chain['strikes']),
                     expiries[inst])
            continue

        snap = score_snapshot(prev, chain, lot_size=INSTRUMENTS[inst]['lot_size'])
        state = states.setdefault(inst, empty_state())
        state = cumulative_state_update(state, snap)

        # Conviction pipeline (May-2026): anomaly-gate → EMA accumulator →
        # alert only on a buildup STATE change (band escalation / flip).
        state = update_conviction(state, snap,
                                  alpha=CONV_ALPHA, decay=CONV_DECAY,
                                  z_min=CONV_Z_MIN)
        states[inst] = state

        sig = conviction_signal(state)
        _write_state(inst, snap, state, sig)

        if sig is not None:
            _maybe_alert(inst, sig, snap, state, token, chats)

        prev_snaps[inst] = chain


def daemon() -> None:
    """Main loop: poll every POLL_SECS until 15:30 IST.

    Resilience features:
      - Per-call timeouts (API_TIMEOUT_SEC) via thread-pool wrapper
      - Periodic re-auth (every AUTH_REFRESH_SEC) to survive token expiry
      - Immediate re-auth on detected auth errors
      - Exponential backoff (BACKOFF_SCHEDULE) on consecutive cycle failures
      - Atomic state-file writes (no corruption on crash)
      - Top-level try/except in main loop — single bad cycle won't kill daemon
    """
    _setup_logging()
    log.info("Option flow daemon starting — poll=%ds, cooldown=%ds, "
             "instruments=%s, api_timeout=%ds, auth_refresh=%ds",
             POLL_SECS, ALERT_COOLDOWN_SEC, list(INSTRUMENTS.keys()),
             API_TIMEOUT_SEC, AUTH_REFRESH_SEC)

    g = _get_groww()
    last_auth_ts = time.time()
    token, chats = _telegram_macro()
    if not token or not chats:
        log.warning("Telegram MACRO not configured — alerts will be skipped, "
                    "state file will still update")

    prev_snaps: dict[str, dict]  = {}
    states:     dict[str, dict]  = {}
    expiries:   dict[str, str]   = {}
    oi_accum:       dict[str, dict] = {}   # {inst: {strike: {'CE':[...], 'PE':[...]}}}
    last_oi_minute: dict[str, object] = {} # {inst: pd.Timestamp(minute)}

    last_heartbeat = time.time()
    last_oi_persist = time.time()
    consecutive_failures = 0   # for backoff schedule

    while True:
        if not _is_market_now():
            t = datetime.now(IST).time()
            if t > MARKET_CLOSE:
                log.info("Market closed (>15:30 IST) — exiting")
                # Final OI flush so the last minutes of the session land on disk.
                for inst in INSTRUMENTS:
                    _persist_option_oi(inst, oi_accum)
                if ALERT_MODE == 'summary':
                    _send_eod_summary(states, token, chats)
                _API_EXECUTOR.shutdown(wait=False)
                return
            log.info("Pre-market (now %s) — sleeping 30s", t)
            time.sleep(30)
            continue

        # Scheduled re-auth — survive Groww token expiry mid-session
        if time.time() - last_auth_ts >= AUTH_REFRESH_SEC:
            try:
                g = _get_groww()
                last_auth_ts = time.time()
                log.info("Periodic re-auth OK (every %ds)", AUTH_REFRESH_SEC)
            except Exception as e:
                log.warning("Periodic re-auth failed (will retry next cycle): %s", e)

        cycle_failed = False
        try:
            oneshot(g, list(INSTRUMENTS.keys()),
                    prev_snaps, states, expiries, token, chats,
                    oi_accum, last_oi_minute)
            # Reset failure counter on a clean cycle
            if consecutive_failures > 0:
                log.info("Recovered after %d consecutive failures",
                         consecutive_failures)
            consecutive_failures = 0
        except Exception as e:
            cycle_failed = True
            consecutive_failures += 1
            # Auth error → force re-auth ASAP (don't wait for AUTH_REFRESH_SEC)
            if _is_auth_error(e):
                log.warning("Auth error in oneshot — re-authenticating: %s", e)
                try:
                    g = _get_groww()
                    last_auth_ts = time.time()
                except Exception as e2:
                    log.exception("Forced re-auth failed: %s", e2)
            else:
                log.exception("oneshot failed (%d consecutive): %s",
                              consecutive_failures, e)

        # Heartbeat — also surfaces failure counter
        # NB: comma grouping (`,`) is NOT valid in %-style format — use f-string.
        if time.time() - last_heartbeat >= HEARTBEAT_SECS:
            for inst in INSTRUMENTS:
                s = states.get(inst, {})
                log.info(f"[{inst}] heartbeat "
                         f"conviction={float(s.get('conviction', 0)):+.2f} "
                         f"band={int(s.get('conv_band', 0)):+d} "
                         f"anomalies={int(s.get('anomaly_count', 0))} "
                         f"failures={consecutive_failures}")
            last_heartbeat = time.time()

        # Persist per-minute OI to option_oi_1m_<INST>.pkl on a cadence so the
        # viewer's option S&R walls stay fresh through the session.
        if time.time() - last_oi_persist >= OI_PERSIST_SECS:
            for inst in INSTRUMENTS:
                _persist_option_oi(inst, oi_accum)
            last_oi_persist = time.time()

        # Apply backoff schedule on consecutive failures
        sleep_secs = _backoff_sleep(consecutive_failures)
        if cycle_failed and sleep_secs > POLL_SECS:
            log.info("Backoff: sleeping %ds (failures=%d)",
                     sleep_secs, consecutive_failures)
        time.sleep(sleep_secs)


def test() -> None:
    """Send one synthetic alert to verify MACRO routing."""
    _setup_logging()
    token, chats = _telegram_macro()
    if not token or not chats:
        log.warning("MACRO Telegram not configured"); return
    fake_snap = {
        'spot': 24000.0, 'net': +1450, 'bull': 1450, 'bear': 0,
        'top_strikes': [
            {'strike':24000,'side':'CE','state':'short_call_cover','sign':+1,'weight':520},
            {'strike':23800,'side':'PE','state':'short_put_build','sign':+1,'weight':430},
        ],
        'state_counts': {'short_call_cover': 3, 'short_put_build': 2},
    }
    fake_state = {'cvd': 18500, 'conviction': +2.4, 'conv_band': +2,
                  'conv_peak': +2.8, 'anomaly_count': 14, 'consec_anom_n': 6,
                  'history': [800, 900, 1100, 1300, 1450]}
    text = '🧪 <b>SMOKE TEST</b>\n' + _format_alert('NIFTY', 'conviction_escalate',
                                                     fake_snap, fake_state)
    for c in chats:
        ok = tg_send(token, c, text)
        log.info("test → %s: %s", c, "OK" if ok else "FAIL")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=('test', 'oneshot', 'daemon'), default='daemon')
    args = ap.parse_args()
    if   args.mode == 'test':    test()
    elif args.mode == 'daemon':  daemon()
    elif args.mode == 'oneshot':
        # Single in-market cycle, then exit (for cron testing)
        _setup_logging()
        if not _is_market_now():
            log.info("Outside market hours — skipping oneshot")
            return
        g = _get_groww()
        token, chats = _telegram_macro()
        prev, states, exp = {}, {}, {}
        oi_accum, last_oi_minute = {}, {}
        oneshot(g, list(INSTRUMENTS.keys()), prev, states, exp, token, chats,
                oi_accum, last_oi_minute)
        for inst in INSTRUMENTS:
            _persist_option_oi(inst, oi_accum)


if __name__ == '__main__':
    main()
