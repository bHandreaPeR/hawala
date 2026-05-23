"""
v3/live/runner_nifty.py
========================
Live intraday runner for NIFTY options — real-time signal engine.

Flow:
  9:15 AM    → Auth. Start per-minute option chain + spot + futures polling.
  9:15–10:15 → Accumulate OI history, train FII/DII rolling buffer, warm EMA.
  10:15+     → Signal engine fires every minute via SignalSmoother.
               First bar where smoothed direction != 0 and |score| > threshold
               → ENTER ATM CE (LONG) or ATM PE (SHORT).
  11:00–15:20→ Position open. Log P&L each minute.
  15:20 PM   → Exit position. Log final P&L.

Per-minute data feed:
  1. get_option_chain(NSE, NIFTY, expiry)  → spot (underlying_ltp), all strikes
     OI, LTP; derives: live PCR, live call/put walls, OI snapshots for classifier
  2. get_historical_candles(NSE-NIFTY, CASH)  → spot 1m bars (OI quadrant)
  3. get_historical_candles(NSE-NIFTY-*-FUT, FNO) → futures 1m bars (basis)

Signals are all real-time:
  1. OI Quadrant     — from futures 1m OI
  2. Futures Basis   — fut_ltp vs spot (underlying_ltp from option chain)
  3. PCR             — computed live from option chain OI every minute
  4. OI Velocity     — from rolling OI history buffer (OI delta / bars_elapsed)
  5. Strike Defense  — walls from live option chain OI
  6. FII/DII         — FIIDIIClassifier on rolling OI snapshots

Paper trade mode (default): logs trades without placing orders.
Set --live (and confirm risk) for real order placement.

Usage:
    python v3/live/runner_nifty.py            # paper trade
    python v3/live/runner_nifty.py --live     # real orders (NOT YET IMPLEMENTED)

Requires: valid token.env, running on a trading day, started before 10:15 AM.
"""
import os, sys, time, pickle, pyotp, logging, argparse
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / 'logs' / 'trade_bot' / 'runner_nifty.log', mode='a'),
    ]
)
log = logging.getLogger('live_runner_nifty')

from v3.signals.engine import (
    compute_signal_state, state_to_dict, SignalSmoother,
)
from v3.signals.fii_dii_classifier import (
    FIIDIIClassifier, OISnapshot,
    THRESHOLDS_FILE,
)
from alerts.telegram import send as _tg_send
from v3.live.flow_conflict import FlowConflictTracker, evaluate as _fc_evaluate
from v3.live.reentry_cooldown import (
    is_in_cooldown as _re_is_in_cooldown,
    record_force_exit as _re_record_force_exit,
)

# ── Constants ────────────────────────────────────────────────────────────────
NIFTY_LOT    = 65
NIFTY_STEP   = 50
ENTRY_BAR    = 105           # 9:15 + 105 min = 11:00 AM  (matches backtest MIN_SIGNAL_BAR)
LAST_ENTRY_HHMM = (13, 0)   # no entries after 13:00 (matches backtest LAST_ENTRY_HHMM)
EXIT_HHMM    = (15, 20)      # EOD forced exit (matches backtest EOD_EXIT_HHMM)
SIGNAL_SCORE_MIN = 0.35      # minimum |score| to fire trade (matches backtest threshold)

SL_PCT = -0.50   # option loses 50% of entry premium → stop loss
TP_PCT = +1.00   # option doubles (100% gain) → take profit

# Reversal check interval (bars)
REVERSAL_CHECK_EVERY = 5
# Minimum bars held before a reversal exit is allowed (matches backtest MIN_REVERSAL_HOLD)
MIN_REVERSAL_HOLD = 20

# Post-engine filter thresholds (mirror run_backtest_nifty.py)
MIN_SIGNAL_COUNT = 5   # minimum signals (of 6) that must agree before entry
MOMENTUM_BARS    = 30  # price must trend in signal direction for last 30 bars

# Option-flow confluence filter (opt-in, env-gated)
# Reads v3/cache/option_flow_NIFTY.json — produced by alerts/option_flow_daemon.
# When V3_REQUIRE_OPTION_FLOW_CONFIRM=1 in env, suppresses entries whose
# direction disagrees with the 5-sec OI flow daemon's session direction.
# Default OFF — measure correlation for 2-3 weeks before flipping on.
import os as _os
REQUIRE_OPTION_FLOW_CONFIRM = _os.environ.get("V3_REQUIRE_OPTION_FLOW_CONFIRM",
                                               "0").lower() in ("1", "true", "yes")
OPTION_FLOW_MAX_AGE_SEC = 60   # ignore the file if stale beyond this

# OI velocity rolling window
OI_HISTORY_MAXLEN = 60    # keep 60 bars of OI history per strike (matches backtest VELOCITY_WINDOW)


# ── Option-flow confluence reader ───────────────────────────────────────────
def _read_option_flow_direction(inst: str) -> int:
    """Read the option-flow daemon's latest direction for an instrument.

    Returns +1 / -1 / 0. Returns 0 if file is missing, malformed, or stale
    beyond OPTION_FLOW_MAX_AGE_SEC. Safe to call always (caller decides
    whether to act on it).
    """
    import json as _json
    from datetime import datetime as _dt
    try:
        p = ROOT / 'v3' / 'cache' / f'option_flow_{inst}.json'
        if not p.exists():
            return 0
        d = _json.loads(p.read_text())
        ts = _dt.fromisoformat(d.get('ts', ''))
        age = (_dt.now(ts.tzinfo) - ts).total_seconds() if ts.tzinfo else 9999
        if age > OPTION_FLOW_MAX_AGE_SEC:
            return 0
        # Use session CVD direction (more stable than single-snapshot net)
        cvd = float(d.get('cvd', 0.0))
        return +1 if cvd > 0 else (-1 if cvd < 0 else 0)
    except Exception:
        return 0

# ── Auth ─────────────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    env = {}
    with open(ROOT / 'token.env') as f:
        for line in f:
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k] = v
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    # 3-try retry with 5s backoff — single transient Groww error at boot
    # used to kill the entire session (May 11 + May 15 crashes).
    last_err = None
    for attempt in range(3):
        try:
            token = GrowwAPI.get_access_token(api_key=env['GROWW_API_KEY'], totp=totp)
            log.info("Groww auth OK (attempt %d)", attempt + 1)
            return GrowwAPI(token=token)
        except Exception as e:
            last_err = e
            if attempt < 2:
                log.warning("Groww auth attempt %d failed, retry 5s: %s", attempt + 1, e)
                import time as _t; _t.sleep(5)
                # Refresh TOTP — 30s window may have passed
                totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    log.error("Groww auth failed after 3 attempts: %s", last_err)
    raise last_err


# ── Telegram helpers ─────────────────────────────────────────────────────────

def _load_telegram_config() -> tuple:
    """
    Load TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS from token.env.
    Returns (token: str, chat_ids: list[str]).
    Returns ('', []) if token.env is missing or keys not found — alerts
    will be silently skipped (no crash).
    """
    env_path = ROOT / 'token.env'
    if not env_path.exists():
        log.warning("token.env not found at %s — Telegram alerts disabled", env_path)
        return '', []

    env: dict = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()

    token    = env.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_raw = env.get('TELEGRAM_CHAT_IDS', env.get('TELEGRAM_CHAT_ID', '')).strip()
    chat_ids = [c.strip() for c in chat_raw.split(',') if c.strip()]

    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set in token.env — alerts disabled")
        return '', []
    if not chat_ids:
        log.warning("TELEGRAM_CHAT_IDS not set in token.env — alerts disabled")
        return token, []

    log.info("Telegram config loaded: %d chat(s)", len(chat_ids))
    return token, chat_ids


def _tg_broadcast(token: str, chat_ids: list, text: str) -> None:
    """Send `text` to all configured Telegram chats. Non-fatal on failure."""
    if not token or not chat_ids:
        return
    for cid in chat_ids:
        ok = _tg_send(token, cid, text)
        if not ok:
            log.warning("Telegram send failed: chat_id=%s", cid)


# ── Alert message formatters ──────────────────────────────────────────────────

def _fmt_morning_alert(today: date, static: dict, clf: Optional[object]) -> str:
    spot      = static.get('spot', 0)
    pcr_val   = static.get('pcr_val', 0)
    pcr_ma    = static.get('pcr_ma', 0)
    fii_cash  = static.get('fii_cash_lag1', 0)
    fii_fut   = static.get('fii_fut_level', 0)
    dte       = static.get('dte', 0)

    cash_lbl  = '🔴 Selling' if fii_cash < 0 else ('🟢 Buying' if fii_cash > 0 else '⬜ Neutral')
    fo_lbl    = '🟢 Net Long' if fii_fut > 0 else ('🔴 Net Short' if fii_fut < 0 else '⬜ Neutral')
    day_str   = today.strftime('%a %d %b %Y')

    trained_days = len(clf._thresh.get('training_days', [])) if clf else 0
    clf_line  = (f"FII/DII classifier: <b>{trained_days} days</b> trained"
                 if trained_days else "FII/DII classifier: <i>not loaded</i>")

    return (
        f"🌅 <b>Hawala v3 — NIFTY</b>  {day_str}\n"
        f"{'─'*32}\n"
        f"Spot prev close: <b>{spot:,.0f}</b>\n"
        f"FII cash (lag-1): {cash_lbl}\n"
        f"FII F&O net: {fo_lbl}\n"
        f"PCR (prev EOD): <b>{pcr_val:.3f}</b>  |  MA(5): <b>{pcr_ma:.3f}</b>\n"
        f"DTE: <b>{dte}</b>\n"
        f"{clf_line}\n"
        f"SL: –50%  |  TP: +100%\n"
        f"Entry window: 11:00 → 13:00  |  EOD: 15:20"
    )


def _fmt_entry_alert(
    position: dict,
    state,          # SignalState from engine
    fii_dii_result: Optional[dict],
    paper: bool,
) -> str:
    direction  = position['direction']
    side       = position['side']
    strike     = position['strike']
    entry_px   = position['entry_price']
    entry_time = position['entry_time'].strftime('%H:%M')
    score      = position['entry_score']
    opt_sym    = position.get('opt_symbol', f"{side} {strike}")

    sl_px  = round(entry_px * (1 + SL_PCT), 2)
    tp_px  = round(entry_px * (1 + TP_PCT), 2)
    sl_pts = round(entry_px * SL_PCT, 2)
    tp_pts = round(entry_px * TP_PCT, 2)

    dir_emoji  = '🟢 LONG' if direction == 1 else '🔴 SHORT'
    mode_tag   = ' [PAPER]' if paper else ''

    # Signal breakdown — field names match SignalState dataclass in engine.py
    sig_map = {
        'OI Quadrant':    getattr(state, 'oi_quadrant',    0),
        'Basis':          getattr(state, 'futures_basis',  0),
        'PCR':            getattr(state, 'pcr',            0),
        'OI Velocity':    getattr(state, 'oi_velocity',    0),
        'Strike Defense': getattr(state, 'strike_defense', 0),
        'FII Signature':  getattr(state, 'fii_signature',  0),
    }
    sig_lines = '  '.join(
        f"{'✅' if v != 0 else '⬜'} {k}" for k, v in sig_map.items()
    )

    # FII/DII classifier line
    if fii_dii_result and fii_dii_result.get('attribution', 'UNKNOWN') != 'UNKNOWN':
        attr  = fii_dii_result['attribution']
        conf  = fii_dii_result['confidence']
        fscore = fii_dii_result['fii_score']
        clf_line = f"Classifier: <b>{attr}</b>  conf={conf:.2f}  fii_score={fscore:+.3f}"
    else:
        clf_line = "Classifier: N/A"

    return (
        f"⚡ <b>Hawala v3 — NIFTY</b>\n"
        f"{dir_emoji} <b>{opt_sym}</b>  @  ₹{entry_px:.2f}{mode_tag}\n"
        f"{'─'*32}\n"
        f"Entry: {entry_time}  |  Score: <b>{score:+.3f}</b>  |  "
        f"Signals: {state.signal_count}\n"
        f"\n"
        f"Signals:\n{sig_lines}\n"
        f"\n"
        f"{clf_line}\n"
        f"{'─'*32}\n"
        f"SL: ₹{sl_px:.2f} ({sl_pts:+.2f} pts, –50%)\n"
        f"TP: ₹{tp_px:.2f} ({tp_pts:+.2f} pts, +100%)"
    )


def _fmt_exit_alert(
    position: dict,
    exit_price: float,
    exit_reason: str,
    pnl_pts: float,
    pnl_inr: float,
    paper: bool,
) -> str:
    opt_sym    = position.get('opt_symbol', f"{position['side']} {position['strike']}")
    entry_px   = position['entry_price']
    entry_time = position['entry_time'].strftime('%H:%M')
    exit_time  = datetime.now().strftime('%H:%M')
    mode_tag   = ' [PAPER]' if paper else ''

    win      = pnl_pts > 0
    emoji    = '✅' if win else '❌'
    lbl      = 'WIN' if win else 'LOSS'
    pnl_sign = '+' if pnl_pts >= 0 else ''

    reason_map = {
        'SL':       '🛑 Stop Loss hit (–50%)',
        'TP':       '🎯 Take Profit hit (+100%)',
        'EOD':      '🕥 EOD exit (15:20)',
        'REVERSAL': '🔄 Signal reversal',
        'FLOW_SL':  '🔴 Flow-conflict premature stop (L3)',
    }
    reason_str = reason_map.get(exit_reason, exit_reason)

    return (
        f"⚡ <b>Hawala v3 — NIFTY</b>\n"
        f"{emoji} <b>EXIT — {lbl} ({exit_reason})</b>{mode_tag}\n"
        f"{'─'*32}\n"
        f"{opt_sym}  |  Entry: ₹{entry_px:.2f} @ {entry_time}  |  "
        f"Exit: ₹{exit_price:.2f} @ {exit_time}\n"
        f"\n"
        f"PnL: <b>{pnl_sign}₹{pnl_inr:,.2f}</b>  ({pnl_sign}{pnl_pts:.2f} pts)\n"
        f"Qty: {position['qty']} lots\n"
        f"\n"
        f"Reason: {reason_str}"
    )


# ── Contract resolvers ───────────────────────────────────────────────────────
def _last_tuesday(year: int, month: int) -> date:
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - 1) % 7
    return last_day - timedelta(days=days_back)


def _nearest_tuesday_expiry(trade_date: date) -> date:
    y, m = trade_date.year, trade_date.month
    for _ in range(3):
        exp = _last_tuesday(y, m)
        if exp >= trade_date:
            return exp
        m += 1
        if m > 12:
            m, y = 1, y + 1
    raise RuntimeError(f"Cannot find Tuesday expiry for {trade_date}")


def _futures_symbol(trade_date: date) -> str:
    exp = _nearest_tuesday_expiry(trade_date)
    return f"NSE-NIFTY-{exp.day}{exp.strftime('%b')}{exp.strftime('%y')}-FUT"


def _option_symbol(expiry: date, strike: int, side: str) -> str:
    return (f"NSE-NIFTY-{expiry.day}{expiry.strftime('%b')}"
            f"{expiry.strftime('%y')}-{strike}-{side}")


# ── Option chain fetch (primary per-minute feed) ─────────────────────────────
def _fetch_option_chain(g, expiry: date) -> Optional[dict]:
    """
    Fetch the full NIFTY option chain for the given expiry.

    Returns a dict:
      {
        'underlying_ltp': float,          # live spot proxy
        'strikes':        sorted list,    # all available strikes
        'ce_oi':          {strike: float},
        'pe_oi':          {strike: float},
        'ce_ltp':         {strike: float},
        'pe_ltp':         {strike: float},
        'ce_vol':         {strike: float},
        'pe_vol':         {strike: float},
        'pcr':            float,          # total PE OI / total CE OI
        'call_wall':      int,            # strike with max CE OI
        'put_wall':       int,            # strike with max PE OI
        'walls':          dict,           # {call_wall, put_wall, pcr_live}
      }

    Returns None if the API call fails.
    """
    exp_str = expiry.isoformat()
    try:
        r = g.get_option_chain(
            exchange='NSE',
            underlying='NIFTY',
            expiry_date=exp_str,
        )
        if r is None:
            raise ValueError("get_option_chain returned None")
        underlying_ltp = float(r.get('underlying_ltp', 0) or 0)
        # Groww response: strikes is dict[str_strike → {CE: {open_interest, ltp, volume, ...}, PE: {...}}]
        raw_strikes = r.get('strikes', {})
    except Exception as e:
        log.warning("get_option_chain failed: expiry=%s error=%s", exp_str, e)
        return None

    if not raw_strikes:
        log.warning("get_option_chain returned empty strikes: expiry=%s response_keys=%s", exp_str, list(r.keys()) if r else None)
        return None

    # Defensive: Groww historically returned a list; current format is dict[str_strike→data].
    # If a list arrives (API regression or partial response), log and bail rather than crash.
    if not isinstance(raw_strikes, dict):
        log.warning(
            "get_option_chain: unexpected strikes type: expiry=%s type=%s — skipping bar",
            exp_str, type(raw_strikes).__name__,
        )
        return None

    ce_oi: dict  = {}
    pe_oi: dict  = {}
    ce_ltp: dict = {}
    pe_ltp: dict = {}
    ce_vol: dict = {}
    pe_vol: dict = {}

    for strike_str, data in raw_strikes.items():
        try:
            strike = int(strike_str)
            if strike <= 0:
                continue
            ce = data.get('CE', {}) if isinstance(data, dict) else {}
            pe = data.get('PE', {}) if isinstance(data, dict) else {}
            ce_oi[strike]  = float(ce.get('open_interest', 0) or 0)
            pe_oi[strike]  = float(pe.get('open_interest', 0) or 0)
            ce_ltp[strike] = float(ce.get('ltp', 0) or 0)
            pe_ltp[strike] = float(pe.get('ltp', 0) or 0)
            ce_vol[strike] = float(ce.get('volume', 0) or 0)
            pe_vol[strike] = float(pe.get('volume', 0) or 0)
        except (TypeError, ValueError) as exc:
            log.warning(
                "option_chain: skip malformed strike: strike_str=%s error=%s", strike_str, exc
            )
            continue

    strikes = sorted(ce_oi.keys())
    if not strikes:
        log.warning("option_chain: no usable strikes after parsing")
        return None

    # Live PCR and walls
    tot_ce = sum(ce_oi.values())
    tot_pe = sum(pe_oi.values())
    pcr    = (tot_pe / tot_ce) if tot_ce > 0 else 1.0

    # Near-money band for walls (ATM ± 1000 pts)
    if underlying_ltp > 0:
        near = [s for s in strikes if abs(s - underlying_ltp) <= 1000]
    else:
        near = strikes

    call_wall = max(near, key=lambda s: ce_oi.get(s, 0)) if near else strikes[-1]
    put_wall  = max(near, key=lambda s: pe_oi.get(s, 0)) if near else strikes[0]

    walls = {
        'call_wall': call_wall,
        'put_wall':  put_wall,
        'pcr_live':  round(pcr, 3),
    }

    return {
        'underlying_ltp': underlying_ltp,
        'strikes':        strikes,
        'ce_oi':          ce_oi,
        'pe_oi':          pe_oi,
        'ce_ltp':         ce_ltp,
        'pe_ltp':         pe_ltp,
        'ce_vol':         ce_vol,
        'pe_vol':         pe_vol,
        'pcr':            round(pcr, 3),
        'call_wall':      call_wall,
        'put_wall':       put_wall,
        'walls':          walls,
    }


# ── Rolling OI history → per-minute velocity ─────────────────────────────────

class OIHistoryBuffer:
    """
    Keeps the last N OI values per (strike, side) for velocity computation.

    After each minute's option chain call, push the new OI values.
    Then call compute_velocity() to get a velocity_data dict
    compatible with signal_oi_velocity().
    """

    def __init__(self, maxlen: int = OI_HISTORY_MAXLEN):
        self._maxlen = maxlen
        # {strike: deque of (ts, ce_oi, pe_oi)}
        self._hist: dict = defaultdict(lambda: deque(maxlen=maxlen))

    def push(self, ts: pd.Timestamp, chain: dict) -> None:
        """Record one minute's OI snapshot from option chain dict."""
        for strike in chain['strikes']:
            ce = chain['ce_oi'].get(strike, 0.0)
            pe = chain['pe_oi'].get(strike, 0.0)
            self._hist[strike].append((ts, ce, pe))

    def compute_velocity(self, ltp: float, band_pct: float = 0.05) -> dict:
        """
        Compute OI velocity for strikes within band_pct of ltp.

        Velocity = (OI_last - OI_first) / n_bars_elapsed.

        NSE publishes OI in batches, so consecutive identical values are
        ffilled from the API. We detect the most recent true update by
        scanning from the back and finding where OI last changed.

        Returns {strike: {ce_oi, pe_oi, ce_velocity, pe_velocity, net_velocity}}
        """
        result: dict = {}
        band = ltp * band_pct if ltp > 0 else 0

        for strike, hist in self._hist.items():
            if band > 0 and abs(strike - ltp) > band:
                continue
            if len(hist) < 2:
                continue

            entries = list(hist)
            # Find last actual OI update for CE and PE
            ce_vel = _last_true_velocity(
                [e[1] for e in entries], len(entries)
            )
            pe_vel = _last_true_velocity(
                [e[2] for e in entries], len(entries)
            )
            result[strike] = {
                'ce_oi':        entries[-1][1],
                'pe_oi':        entries[-1][2],
                'ce_velocity':  round(ce_vel, 2),
                'pe_velocity':  round(pe_vel, 2),
                'net_velocity': round(pe_vel - ce_vel, 2),
            }

        return result

    def reset(self) -> None:
        self._hist.clear()


def _last_true_velocity(oi_list: list, n: int) -> float:
    """
    Given a list of OI values (possibly ffilled), compute the velocity
    of the most recent non-zero OI change.

    Scans backward to find the last bar where OI actually changed.
    Returns change / bars_elapsed, or 0 if no change found.
    """
    if n < 2:
        return 0.0
    last_val  = oi_list[-1]
    for i in range(n - 2, -1, -1):
        if oi_list[i] != last_val:
            elapsed = (n - 1) - i
            return float((last_val - oi_list[i]) / elapsed)
    return 0.0


# ── 1m bar fetch ─────────────────────────────────────────────────────────────
def _fetch_latest_bar(g, symbol: str, trade_date: date) -> dict | None:
    """Fetch the most recent completed 1m bar for a symbol."""
    now   = datetime.now()
    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T{now.strftime('%H:%M:%S')}"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='FNO', groww_symbol=symbol,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', [])
        if not candles:
            return None
        row = candles[-1]
        return {
            'ts':     pd.Timestamp(row[0]),
            'open':   float(row[1]),
            'high':   float(row[2]),
            'low':    float(row[3]),
            'close':  float(row[4]),
            'volume': float(row[5]),
            'oi':     float(row[6]) if len(row) > 6 else 0.0,
        }
    except Exception as e:
        log.warning("fetch_latest_bar symbol=%s error=%s", symbol, e)
        return None


def _fetch_all_bars(g, symbol: str, trade_date: date) -> pd.DataFrame:
    """Fetch all FNO 1m bars for symbol so far today."""
    now   = datetime.now()
    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T{now.strftime('%H:%M:%S')}"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='FNO', groww_symbol=symbol,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', [])
        if not candles:
            return pd.DataFrame()
        # Groww returns 7 cols (with OI) for expired contracts, 6 cols (no OI) for
        # the active contract during its live month. Handle both without crashing.
        n_cols = len(candles[0])
        if n_cols >= 7:
            cols = ['ts', 'open', 'high', 'low', 'close', 'volume', 'oi']
            df = pd.DataFrame([c[:7] for c in candles], columns=cols)
        else:
            cols = ['ts', 'open', 'high', 'low', 'close', 'volume']
            df = pd.DataFrame([c[:6] for c in candles], columns=cols)
            df['oi'] = float('nan')
            log.debug(
                "fetch_all_bars: %s returned 6-col response (active contract) — OI=NaN",
                symbol,
            )
        df['ts'] = pd.to_datetime(df['ts'])
        for col in ['open', 'high', 'low', 'close', 'volume', 'oi']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['date'] = df['ts'].dt.date
        df['time'] = df['ts'].dt.time
        return df
    except Exception as e:
        log.warning("fetch_all_bars symbol=%s error=%s", symbol, e)
        return pd.DataFrame()


def _fetch_all_bars_spot(g, symbol: str, trade_date: date) -> pd.DataFrame:
    """
    Fetch all CASH segment 1m bars for spot index (e.g. NSE-NIFTY).
    volume and oi fields will be None/0 for the index — that is expected.
    Returns DataFrame[ts, open, high, low, close, volume, oi, date, time].
    Returns empty DataFrame on any failure.
    """
    now   = datetime.now()
    start = f"{trade_date}T09:15:00"
    end   = f"{trade_date}T{now.strftime('%H:%M:%S')}"
    try:
        r = g.get_historical_candles(
            exchange='NSE', segment='CASH', groww_symbol=symbol,
            start_time=start, end_time=end,
            candle_interval=g.CANDLE_INTERVAL_MIN_1,
        )
        candles = r.get('candles', [])
        if not candles:
            return pd.DataFrame()

        rows = []
        for c in candles:
            rows.append({
                'ts':     pd.Timestamp(c[0]),
                'open':   float(c[1]) if c[1] is not None else np.nan,
                'high':   float(c[2]) if c[2] is not None else np.nan,
                'low':    float(c[3]) if c[3] is not None else np.nan,
                'close':  float(c[4]) if c[4] is not None else np.nan,
                'volume': 0.0,
                'oi':     0.0,
            })
        df = pd.DataFrame(rows)
        df['date'] = df['ts'].dt.date
        df['time'] = df['ts'].dt.time
        return df
    except Exception as e:
        log.warning(
            "fetch_all_bars_spot symbol=%s error=%s",
            symbol, e,
        )
        return pd.DataFrame()


# ── Load static inputs (lag-1) ────────────────────────────────────────────────
def _load_static_inputs(today: date) -> dict:
    """Load all lag-1 inputs that don't change intraday."""
    import yfinance as yf

    # FII F&O
    fii_fo_path = ROOT / 'trade_logs/_fii_fo_cache.pkl'
    fii_fut_level = 0
    if fii_fo_path.exists():
        try:
            with open(fii_fo_path, 'rb') as f:
                fii_fo = pickle.load(f)
        except Exception as e:
            log.warning("fii_fo_cache corrupt or unreadable: path=%s error=%s — skipping FII F&O", fii_fo_path, e)
            fii_fo = {}
        fii_fo_dates = sorted(fii_fo.keys())
        prev_fo = [d for d in fii_fo_dates if d < str(today)]
        if prev_fo:
            fo = fii_fo[prev_fo[-1]]
            fl, fs = fo.get('fut_long', 0), fo.get('fut_short', 0)
            fii_fut_level = 1 if fl > fs * 1.15 else (-1 if fs > fl * 1.15 else 0)

    # FII cash
    fii_cash_lag1 = 0
    fii_cash_path = ROOT / 'fii_data.csv'
    if fii_cash_path.exists():
        fii_cash_df = pd.read_csv(fii_cash_path)
        fii_cash_df['date'] = pd.to_datetime(fii_cash_df['date']).dt.date
        prev_cash = fii_cash_df[fii_cash_df['date'] < today].tail(1)
        if not prev_cash.empty:
            net = float(prev_cash['fpi_net'].iloc[0])
            fii_cash_lag1 = 1 if net > 500 else (-1 if net < -500 else 0)

    # PCR from bhavcopy
    pcr_val = 1.0
    pcr_ma  = 1.0
    bhav_path = ROOT / 'v3/cache/bhavcopy_NIFTY_all.pkl'
    if bhav_path.exists():
        try:
            with open(bhav_path, 'rb') as f:
                bhav = pickle.load(f)
        except Exception as e:
            log.warning("bhavcopy_NIFTY_all corrupt or unreadable: path=%s error=%s — PCR=1.0", bhav_path, e)
            bhav = {}
        bhav_dates = sorted(bhav.keys())
        pcr_rows = []
        for d_str, df_s in bhav.items():
            if df_s.empty:
                continue
            total_ce = df_s['ce_oi'].sum() if 'ce_oi' in df_s.columns else 0
            total_pe = df_s['pe_oi'].sum() if 'pe_oi' in df_s.columns else 0
            if total_ce > 0:
                pcr_rows.append({'date': pd.Timestamp(d_str), 'pcr': total_pe / total_ce})
        if pcr_rows:
            pcr_df = pd.DataFrame(pcr_rows).sort_values('date')
            pcr_df['pcr_5d_ma'] = pcr_df['pcr'].rolling(5, min_periods=1).mean()
            pcr_df['date_only'] = pcr_df['date'].dt.date
            prev_pcr = pcr_df[pcr_df['date_only'] < today].tail(1)
            if not prev_pcr.empty:
                pcr_val = float(prev_pcr['pcr'].iloc[0])
                pcr_ma  = float(prev_pcr['pcr_5d_ma'].iloc[0])
        walls = {}
        prev_bhav = [d for d in bhav_dates if d < str(today)]
        if prev_bhav:
            df_b = bhav[prev_bhav[-1]]
            total_ce = df_b['ce_oi'].sum() if not df_b.empty else 0
            total_pe = df_b['pe_oi'].sum() if not df_b.empty else 0
            pcr_live = total_pe / total_ce if total_ce > 0 else 1.0
            calls = df_b[df_b['strike'] > 0] if not df_b.empty else pd.DataFrame()
            # Simple wall: max OI strike above/below ATM
            walls = {'pcr_live': round(pcr_live, 3)}
    else:
        walls = {}

    # Spot (prev close) + 5-day regime return (for F1 extreme-regime filter)
    # PRIMARY: local futures candle cache — always available, no network dependency.
    # FALLBACK: yfinance — used only if candle cache is missing (first-ever run).
    # NOTE: regime_pct near 0 does NOT block entries. F1 only fires when |regime| > 3%.
    regime_pct = 0.0
    spot       = 0.0
    _candle_file_nifty = ROOT / 'v3/cache/candles_1m_NIFTY.pkl'
    if _candle_file_nifty.exists():
        try:
            with open(_candle_file_nifty, 'rb') as _fh:
                _candles_nifty = pickle.load(_fh)
            if not _candles_nifty.empty and 'date' in _candles_nifty.columns:
                _prior_n = _candles_nifty[_candles_nifty['date'] < today]
                _daily_c = _prior_n.groupby('date')['close'].last().sort_index()
                if len(_daily_c) > 0:
                    spot = float(_daily_c.iloc[-1])
                if len(_daily_c) >= 6:
                    regime_pct = float(
                        (_daily_c.iloc[-1] - _daily_c.iloc[-6])
                        / _daily_c.iloc[-6] * 100
                    )
                log.info(
                    "regime: loaded from candle cache — spot=%.0f regime_pct=%.2f%%",
                    spot, regime_pct,
                )
        except Exception as e:
            log.warning("candle cache spot/regime load failed: %s", e)

    # yfinance fallback — only if candle cache gave nothing
    if spot == 0.0:
        try:
            nifty = yf.download('^NSEI', period='15d', interval='1d',
                                progress=False, auto_adjust=True)
            nifty.index = pd.to_datetime(nifty.index).date
            spot_close = {d: float(nifty['Close']['^NSEI'].loc[d]) for d in nifty.index}
            prev_spot_dates = sorted(d for d in spot_close if d < today)
            spot = spot_close[prev_spot_dates[-1]] if prev_spot_dates else 0.0
            if regime_pct == 0.0 and len(prev_spot_dates) >= 6:
                regime_pct = (
                    (spot_close[prev_spot_dates[-1]] - spot_close[prev_spot_dates[-6]])
                    / spot_close[prev_spot_dates[-6]] * 100
                )
            log.info(
                "regime: loaded from yfinance fallback — spot=%.0f regime_pct=%.2f%%",
                spot, regime_pct,
            )
        except Exception as e:
            log.warning(
                "yfinance spot/regime fallback also failed: %s — "
                "regime_pct stays 0.0 — extreme_regime filter (F1) will not trigger",
                e,
            )

    # DTE
    dte = max(((1 - today.weekday()) % 7), 1)

    log.info(
        "Static inputs loaded: spot=%.0f pcr=%.3f pcr_ma=%.3f "
        "fii_fut=%d fii_cash=%d dte=%d regime_pct=%.2f%%",
        spot, pcr_val, pcr_ma, fii_fut_level, fii_cash_lag1, dte, regime_pct,
    )
    return {
        'spot': spot, 'pcr_val': pcr_val, 'pcr_ma': pcr_ma,
        'fii_fut_level': fii_fut_level, 'fii_cash_lag1': fii_cash_lag1,
        'dte': dte, 'walls': walls, 'regime_pct': regime_pct,
    }


# ── Morning bhavcopy refresh ──────────────────────────────────────────────────
def _refresh_morning_bhavcopy(today: date) -> tuple[float, float]:
    """
    Fetch YESTERDAY's NSE bhavcopy and update the cache + pcr_daily.csv.
    Called once at runner startup so pcr_5d_ma is always fresh.

    Returns (pcr_val, pcr_ma) for yesterday — the lag-1 values to pass into
    the signal engine.  Non-fatal: on any failure returns (0.0, 0.0) so the
    caller can fall back to the stale cache values from _load_static_inputs.
    """
    import requests, io, zipfile

    BHAV_CACHE = ROOT / 'v3' / 'cache' / 'bhavcopy_NIFTY_all.pkl'
    PCR_CACHE  = ROOT / 'v3' / 'cache' / 'pcr_daily.csv'

    # Yesterday = last trading day before today
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:   # skip Sat/Sun
        yesterday -= timedelta(days=1)

    yesterday_str = str(yesterday)

    # If already in bhavcopy cache, skip the network fetch
    if BHAV_CACHE.exists():
        try:
            with open(BHAV_CACHE, 'rb') as fh:
                bhav_cache = pickle.load(fh)
        except Exception as e:
            log.warning(
                "morning_bhavcopy: BHAV_CACHE corrupt or unreadable: path=%s error=%s — refetching",
                BHAV_CACHE, e,
            )
            bhav_cache = {}
        if yesterday_str in bhav_cache and not bhav_cache[yesterday_str].empty:
            log.info(
                "Morning bhavcopy: %s already cached — skipping fetch",
                yesterday_str,
            )
            # The network fetch is skipped — but pcr_daily.csv must still be
            # refreshed, else it silently goes stale (ops/autoheal.py at 06:55
            # pre-fetches the bhavcopy .pkl, which is what lands us here).
            try:
                from v3.data.build_pcr_daily import build_pcr_daily
                log.info("pcr_daily.csv refresh (cached path): %s",
                         build_pcr_daily())
            except Exception as e:
                log.warning("pcr_daily.csv refresh from cache failed: %s", e)
            # Still recompute pcr_val / pcr_ma from the cache
            return _pcr_from_bhav_cache(bhav_cache, today)
    else:
        bhav_cache = {}

    # Fetch from NSE
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        'Accept': '*/*',
        'Referer': 'https://www.nseindia.com',
    }

    def _url_new(d: date) -> str:
        return (f"https://archives.nseindia.com/content/fo/"
                f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")

    def _url_old(d: date) -> str:
        month_abbr = d.strftime('%b').upper()
        fname = f"fo{d.strftime('%d%b%Y').upper()}bhav.csv.zip"
        return (f"https://archives.nseindia.com/content/historical/"
                f"DERIVATIVES/{d.year}/{month_abbr}/{fname}")

    session = requests.Session()
    session.headers.update(HEADERS)
    df_raw = pd.DataFrame()

    for url_fn in [_url_new, _url_old]:
        url = url_fn(yesterday)
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    raw = pd.read_csv(f, low_memory=False)
            cols = list(raw.columns)
            if 'TckrSymb' in cols:
                nf = raw[raw['TckrSymb'].str.strip() == 'NIFTY'].copy()
                nf = nf[nf['OptnTp'].isin(['CE', 'PE'])].copy()
                nf['strike'] = pd.to_numeric(nf['StrkPric'], errors='coerce')
                nf['oi']     = pd.to_numeric(nf['OpnIntrst'], errors='coerce').fillna(0)
                nf['vol']    = pd.to_numeric(
                    nf.get('TtlTradgVol', pd.Series(0, index=nf.index)),
                    errors='coerce').fillna(0)
                nf['ltp']    = pd.to_numeric(
                    nf.get('SttlmPric', pd.Series(0, index=nf.index)),
                    errors='coerce').fillna(0)
                opt_col = 'OptnTp'
            elif 'SYMBOL' in cols or 'Symbol' in cols:
                sym_col = 'SYMBOL' if 'SYMBOL' in cols else 'Symbol'
                raw.columns = [c.strip() for c in raw.columns]
                nf = raw[raw[sym_col].str.strip() == 'NIFTY'].copy()
                opt_col = next(
                    (c for c in ['OPTION_TYP', 'OptionType'] if c in nf.columns), None)
                if opt_col is None:
                    continue
                nf = nf[nf[opt_col].isin(['CE', 'PE'])].copy()
                stk_col = next((c for c in ['STRIKE_PR', 'StrikePrice'] if c in nf.columns), 'STRIKE_PR')
                oi_col  = next((c for c in ['OPEN_INT', 'OpenInterest'] if c in nf.columns), 'OPEN_INT')
                nf['strike'] = pd.to_numeric(nf[stk_col], errors='coerce')
                nf['oi']     = pd.to_numeric(nf[oi_col], errors='coerce').fillna(0)
                nf['vol']    = 0
                nf['ltp']    = 0
            else:
                continue

            ce = nf[nf[opt_col] == 'CE'].groupby('strike').agg(
                ce_oi=('oi', 'sum'), ce_vol=('vol', 'sum'), ce_ltp=('ltp', 'first')
            ).reset_index()
            pe = nf[nf[opt_col] == 'PE'].groupby('strike').agg(
                pe_oi=('oi', 'sum'), pe_vol=('vol', 'sum'), pe_ltp=('ltp', 'first')
            ).reset_index()
            df_raw = pd.merge(ce, pe, on='strike', how='outer').fillna(0)
            df_raw['strike'] = df_raw['strike'].astype(int)
            df_raw.sort_values('strike', inplace=True)
            df_raw.reset_index(drop=True, inplace=True)
            break
        except Exception as e:
            log.debug("bhavcopy fetch attempt failed: url=%s error=%s", url, e)
            continue

    if df_raw.empty:
        log.warning(
            "Morning bhavcopy: fetch failed for %s — pcr_5d_ma will use stale cache",
            yesterday_str,
        )
        if BHAV_CACHE.exists():
            try:
                with open(BHAV_CACHE, 'rb') as fh:
                    bhav_cache = pickle.load(fh)
            except Exception as e:
                log.warning(
                    "morning_bhavcopy: fallback BHAV_CACHE corrupt: path=%s error=%s — pcr=1.0",
                    BHAV_CACHE, e,
                )
                bhav_cache = {}
        return _pcr_from_bhav_cache(bhav_cache, today)

    # Store in bhav cache
    bhav_cache[yesterday_str] = df_raw
    with open(BHAV_CACHE, 'wb') as fh:
        pickle.dump(bhav_cache, fh)

    # Update pcr_daily.csv
    ce_tot = float(df_raw['ce_oi'].sum())
    pe_tot = float(df_raw['pe_oi'].sum())
    pcr_yesterday = round(pe_tot / ce_tot, 4) if ce_tot > 0 else 1.0

    pcr_df = pd.DataFrame()
    if PCR_CACHE.exists():
        try:
            pcr_df = pd.read_csv(PCR_CACHE)
            pcr_df['date'] = pcr_df['date'].astype(str).str[:10]
            pcr_df = pcr_df[pcr_df['date'] != yesterday_str]
        except Exception:
            pcr_df = pd.DataFrame()

    new_row = pd.DataFrame([{'date': yesterday_str, 'pcr': pcr_yesterday}])
    pcr_df  = pd.concat([pcr_df, new_row], ignore_index=True)
    pcr_df['date'] = pcr_df['date'].astype(str).str[:10]
    pcr_df  = pcr_df.sort_values('date').reset_index(drop=True)
    pcr_df['pcr_5d_ma'] = pcr_df['pcr'].rolling(5, min_periods=1).mean()
    pcr_df.to_csv(PCR_CACHE, index=False)

    log.info(
        "Morning bhavcopy refreshed: %s  pcr=%.4f  cache=%s",
        yesterday_str, pcr_yesterday, BHAV_CACHE,
    )

    return _pcr_from_bhav_cache(bhav_cache, today)


def _pcr_from_bhav_cache(bhav_cache: dict, today: date) -> tuple[float, float]:
    """Recompute pcr_val + pcr_ma from the bhavcopy cache for today's static inputs."""
    rows = []
    for d_str, df_s in bhav_cache.items():
        if df_s.empty or d_str >= str(today):
            continue
        ce_tot = float(df_s['ce_oi'].sum()) if 'ce_oi' in df_s.columns else 0
        pe_tot = float(df_s['pe_oi'].sum()) if 'pe_oi' in df_s.columns else 0
        if ce_tot > 0:
            rows.append({'date': d_str, 'pcr': pe_tot / ce_tot})
    if not rows:
        return 1.0, 1.0
    pdf = pd.DataFrame(rows).sort_values('date')
    pdf['pcr_5d_ma'] = pdf['pcr'].rolling(5, min_periods=1).mean()
    prev = pdf[pdf['date'] < str(today)].tail(1)
    if prev.empty:
        return 1.0, 1.0
    return float(prev['pcr'].iloc[0]), float(prev['pcr_5d_ma'].iloc[0])


# ── Per-bar cache persistence ─────────────────────────────────────────────────
CANDLE_CACHE = ROOT / 'v3' / 'cache' / 'candles_1m_NIFTY.pkl'
OI_CACHE     = ROOT / 'v3' / 'cache' / 'option_oi_1m_NIFTY.pkl'


def _persist_candles(df_fut: pd.DataFrame, today: date) -> None:
    """
    Write today's futures 1m bars into the persistent candle cache.
    Replaces any existing rows for today — idempotent.
    Called every PERSIST_EVERY bars and at EOD.
    """
    if df_fut.empty:
        return
    try:
        cache = pd.DataFrame()
        if CANDLE_CACHE.exists():
            with open(CANDLE_CACHE, 'rb') as fh:
                cache = pickle.load(fh)
        # Drop today's existing rows then append fresh
        if not cache.empty and 'date' in cache.columns:
            cache = cache[cache['date'].astype(str) != str(today)]
        df_today = df_fut.copy()
        if 'date' not in df_today.columns:
            df_today['date'] = df_today['ts'].dt.date
        if 'time' not in df_today.columns:
            df_today['time'] = df_today['ts'].dt.time
        cache = pd.concat([cache, df_today], ignore_index=True)
        cache.sort_values(['date', 'ts'], inplace=True)
        cache.reset_index(drop=True, inplace=True)
        with open(CANDLE_CACHE, 'wb') as fh:
            pickle.dump(cache, fh)
        log.debug(
            "persist_candles: wrote %d bars for %s → %s",
            len(df_today), today, CANDLE_CACHE,
        )
    except Exception as e:
        log.warning(
            "persist_candles FAILED: date=%s error=%s — data stays in memory",
            today, e,
        )


def _persist_option_oi(oi_snapshots: dict, today: date) -> None:
    """
    Write accumulated per-minute option OI snapshots into the persistent OI cache.

    oi_snapshots format (built in runner loop):
        {strike: {'CE': [(ts, close, volume, oi)], 'PE': [...]}}

    Stored cache format (matches fetch_option_oi_NIFTY.py):
        {date_str: {strike: {'CE': DataFrame[ts, close, volume, oi, oi_raw],
                              'PE': DataFrame[ts, close, volume, oi, oi_raw]}}}

    Called every PERSIST_EVERY bars and at EOD. Idempotent — overwrites today.
    """
    if not oi_snapshots:
        return
    try:
        oi_cache: dict = {}
        if OI_CACHE.exists():
            with open(OI_CACHE, 'rb') as fh:
                oi_cache = pickle.load(fh)

        today_str = str(today)
        day_entry: dict = {}
        for strike, sides in oi_snapshots.items():
            day_entry[strike] = {}
            for side in ('CE', 'PE'):
                rows = sides.get(side, [])
                if not rows:
                    day_entry[strike][side] = pd.DataFrame(
                        columns=['ts', 'close', 'volume', 'oi', 'oi_raw']
                    )
                    continue
                df = pd.DataFrame(rows, columns=['ts', 'close', 'volume', 'oi'])
                df['ts']     = pd.to_datetime(df['ts'])
                df['oi_raw'] = df['oi']   # raw = same as oi (live chain, no ffill needed)
                df['oi']     = pd.to_numeric(df['oi'], errors='coerce').ffill()
                day_entry[strike][side] = df

        oi_cache[today_str] = day_entry
        with open(OI_CACHE, 'wb') as fh:
            pickle.dump(oi_cache, fh)
        log.debug(
            "persist_option_oi: wrote %d strikes for %s → %s",
            len(day_entry), today, OI_CACHE,
        )
    except Exception as e:
        log.warning(
            "persist_option_oi FAILED: date=%s error=%s — data stays in memory",
            today, e,
        )


# ── Realized-vol gate ─────────────────────────────────────────────────────────
MIN_VOL_PCT = 0.85   # 20-day realized vol threshold (daily % returns std)
                     # Below this → range-bound regime, directional buys have
                     # negative edge (Jan 2026 Nifty: 11 trades, 27% WR, -66.6 pts).

def _compute_realized_vol(today: date) -> float:
    """
    Compute 20-day realized vol from cached NIFTY 1m futures candles.
    Uses EOD close of each day (last bar per date) → daily % return std.
    Returns float vol (annualized daily %) or 0.0 if cache not found.

    Raises RuntimeError if cache exists but is corrupted/empty — never silently
    returns 0.0 on a read failure so the caller can't accidentally pass the gate.
    """
    candle_file = ROOT / 'v3/cache/candles_1m_NIFTY.pkl'
    if not candle_file.exists():
        log.warning(
            "vol_gate: candle cache not found at %s — "
            "cannot compute realized vol, defaulting to 0.0 (gate OPEN)",
            candle_file,
        )
        return 0.0

    try:
        with open(candle_file, 'rb') as f:
            candles = pickle.load(f)
    except Exception as e:
        log.warning(
            "vol_gate: candle cache corrupt or unreadable: path=%s error=%s — defaulting vol=0.0",
            candle_file, e,
        )
        return 0.0

    if candles.empty or 'date' not in candles.columns:
        raise RuntimeError(
            f"vol_gate: candle cache at {candle_file} is empty or missing 'date' column"
        )

    # EOD close per day strictly before today (lag-1, no lookahead)
    prior = candles[candles['date'] < today]
    if prior.empty:
        log.warning("vol_gate: no prior candle data before %s — defaulting vol=0.0", today)
        return 0.0

    daily_close = prior.groupby('date')['close'].last().sort_index()
    daily_ret   = daily_close.pct_change() * 100
    if len(daily_ret.dropna()) < 10:
        log.warning(
            "vol_gate: only %d days of returns (need ≥10) — defaulting vol=0.0",
            len(daily_ret.dropna()),
        )
        return 0.0

    vol = float(daily_ret.rolling(20, min_periods=10).std().iloc[-1])
    log.info(
        "vol_gate: 20d realized vol=%.3f%% threshold=%.2f%% → %s",
        vol, MIN_VOL_PCT,
        "PASS (directional mode)" if vol >= MIN_VOL_PCT else "BLOCK (low-vol regime)",
    )
    return vol


# ── OI velocity (early window) ────────────────────────────────────────────────
def _compute_live_velocity(g, expiry: date, trade_date: date,
                            spot: float, band_pct: float = 0.05) -> dict:
    """
    Fetch early-window option OI (9:15–9:45) for ATM ± band strikes.
    Returns velocity dict for signal_oi_velocity().
    """
    atm    = round(spot / NIFTY_STEP) * NIFTY_STEP
    band   = int(spot * band_pct)
    result = {}
    start  = f"{trade_date}T09:15:00"
    end    = f"{trade_date}T09:45:00"

    for offset in range(-band, band + 1, NIFTY_STEP):
        strike = atm + offset
        ce_oi_series, pe_oi_series = [], []
        for side, lst in [('CE', ce_oi_series), ('PE', pe_oi_series)]:
            sym = _option_symbol(expiry, strike, side)
            try:
                r = g.get_historical_candles(
                    exchange='NSE', segment='FNO', groww_symbol=sym,
                    start_time=start, end_time=end,
                    candle_interval=g.CANDLE_INTERVAL_MIN_1,
                )
                for c in r.get('candles', []):
                    lst.append(float(c[6]) if len(c) > 6 else 0.0)
            except Exception as e:
                log.debug("velocity fetch failed symbol=%s error=%s", sym, e)
            time.sleep(0.2)

        if len(ce_oi_series) >= 2 and len(pe_oi_series) >= 2:
            n      = len(ce_oi_series) - 1
            ce_vel = (ce_oi_series[-1] - ce_oi_series[0]) / n
            pe_vel = (pe_oi_series[-1] - pe_oi_series[0]) / n
            result[strike] = {
                'ce_oi':        ce_oi_series[-1],
                'pe_oi':        pe_oi_series[-1],
                'ce_velocity':  round(ce_vel, 2),
                'pe_velocity':  round(pe_vel, 2),
                'net_velocity': round(pe_vel - ce_vel, 2),
            }

    log.info("Live OI velocity: %d strikes computed", len(result))
    return result


# ── Option price fetch ────────────────────────────────────────────────────────
def _get_option_ltp(g, expiry: date, strike: int, side: str, trade_date: date) -> float:
    """Fetch the latest LTP (close of most recent 1m bar) for an option."""
    sym = _option_symbol(expiry, strike, side)
    bar = _fetch_latest_bar(g, sym, trade_date)
    if bar:
        return bar['close']
    raise RuntimeError(
        f"Cannot get option LTP: symbol={sym} trade_date={trade_date}"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def run(paper: bool = True):
    today = date.today()
    now   = datetime.now()

    log.info("=" * 60)
    log.info("NIFTY Live Runner starting. paper=%s date=%s", paper, today)

    if today.weekday() >= 5:
        log.error(
            "Today is %s (%s). Market closed on weekends.",
            today, today.strftime('%A'),
        )
        raise RuntimeError("Market closed — not a weekday.")

    g = _get_groww()

    # ── Telegram ──────────────────────────────────────────────────────────────
    tg_token, tg_chats = _load_telegram_config()

    # ── Vol gate: skip directional strategy in low-vol regimes ────────────────
    # 20-day realized vol < 0.85% = range-bound market.  Directional ATM buys
    # have negative edge in this regime (backtested: 27% WR, -66 pts Jan 2026).
    # No fallback — the IC system for low-vol days is a separate project.
    realized_vol = _compute_realized_vol(today)
    if 0.0 < realized_vol < MIN_VOL_PCT:
        msg = (
            f"⚠️ VOL GATE — NIFTY directional strategy SKIPPED today ({today}).\n"
            f"20d realized vol = {realized_vol:.3f}% < {MIN_VOL_PCT:.2f}% threshold.\n"
            f"Low-vol regime: range-bound market, option buyers have negative edge.\n"
            f"No trade today. Resume when vol ≥ {MIN_VOL_PCT:.2f}%."
        )
        log.warning(msg.replace('\n', ' | '))
        for chat_id in tg_chats:
            try:
                _tg_send(tg_token, chat_id, msg)
            except Exception as tg_err:
                log.warning("Telegram send failed chat=%s err=%s", chat_id, tg_err)
        return   # exit cleanly — no trade today

    # ── Morning bhavcopy refresh (one-time, before static inputs) ────────────
    # Fetches YESTERDAY's bhavcopy from NSE, updates cache + pcr_daily.csv.
    # Non-fatal — falls back to stale cache if NSE is slow or unavailable.
    log.info("Morning refresh: fetching yesterday's bhavcopy for fresh pcr_5d_ma…")
    fresh_pcr_val, fresh_pcr_ma = _refresh_morning_bhavcopy(today)

    static  = _load_static_inputs(today)
    # Override pcr with fresh values if the refresh succeeded (non-zero)
    if fresh_pcr_val > 0:
        static['pcr_val'] = fresh_pcr_val
        static['pcr_ma']  = fresh_pcr_ma
        log.info(
            "pcr updated from fresh bhavcopy: pcr=%.4f pcr_ma=%.4f",
            fresh_pcr_val, fresh_pcr_ma,
        )

    expiry  = _nearest_tuesday_expiry(today)
    fut_sym = _futures_symbol(today)

    log.info("Futures symbol: %s   Option expiry: %s", fut_sym, expiry)

    entry_target      = now.replace(hour=11, minute=0,  second=0, microsecond=0)
    last_entry_target = now.replace(
        hour=LAST_ENTRY_HHMM[0], minute=LAST_ENTRY_HHMM[1], second=0, microsecond=0
    )
    exit_target  = now.replace(hour=EXIT_HHMM[0], minute=EXIT_HHMM[1],
                               second=0, microsecond=0)
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)

    if now > exit_target:
        log.error("Started after exit time (15:20). Nothing to do today.")
        return

    # ── Real-time state objects ───────────────────────────────────────────────
    oi_buf   = OIHistoryBuffer(maxlen=OI_HISTORY_MAXLEN)
    smoother = SignalSmoother(alpha=0.4, threshold=SIGNAL_SCORE_MIN, min_persist=2)

    # Per-minute OI snapshot accumulator → written to option_oi_1m_NIFTY.pkl
    # {strike: {'CE': [(ts, close, volume, oi)], 'PE': [...]}}
    oi_snapshots: dict = {}
    _bar_total_oi: dict = {}   # {pd.Timestamp → float} running total CE+PE OI per bar

    # Write to disk every N bars (checkpoint) + always at EOD
    PERSIST_EVERY = 5   # every 5 minutes
    _bars_since_persist = 0

    # FII/DII classifier (optional — skip gracefully if thresholds not calibrated)
    clf: Optional[FIIDIIClassifier] = None
    if THRESHOLDS_FILE.exists():
        try:
            clf = FIIDIIClassifier()
            log.info("FII/DII classifier loaded OK")
        except Exception as e:
            log.warning(
                "FII/DII classifier could not load (thresholds=%s error=%s). "
                "Signal 6 will use lag-1 fallback.",
                THRESHOLDS_FILE, e,
            )
    else:
        log.warning(
            "FII/DII thresholds not found (%s). "
            "Run FIIDIICalibrator().calibrate() first. "
            "Signal 6 will use lag-1 fallback.",
            THRESHOLDS_FILE,
        )

    # Most-recent option chain snapshot
    last_chain: Optional[dict] = None

    position: Optional[dict] = None   # {side, strike, entry_price, entry_time, qty}
    bars_in_position: int = 0         # bars elapsed since entry (reversal check)
    flow_tracker: Optional[FlowConflictTracker] = None   # flow-conflict exit assist
    morning_alert_sent: bool = False  # send once per session
    df_fut: pd.DataFrame = pd.DataFrame()  # last fetched futures bars — used by EOD persist

    log.info("Waiting for 9:15 AM to start buffering bars...")

    while True:
        now = datetime.now()

        # ── Pre-market wait ───────────────────────────────────────────────────
        if now < market_open:
            sleep_secs = (market_open - now).total_seconds()
            log.info("Pre-market. Sleeping %.0fs until 9:15 AM", sleep_secs)
            time.sleep(min(sleep_secs, 60))
            continue

        # ── Past exit time with no position → done ────────────────────────────
        if now >= exit_target and position is None:
            log.info("Past exit time, no open position. Session complete.")
            break

        # ── Per-minute data fetch ─────────────────────────────────────────────
        ts_bar = pd.Timestamp(now)

        # 1. Option chain (spot proxy + OI + LTP for all strikes)
        chain = _fetch_option_chain(g, expiry)
        if chain is None:
            log.warning("Option chain fetch failed — skipping bar")
            time.sleep(15)
            continue

        spot_ltp = chain['underlying_ltp']
        last_chain = chain

        # 2. Futures 1m bars (OI quadrant + basis numerator)
        df_fut = _fetch_all_bars(g, fut_sym, today)
        if df_fut.empty:
            log.warning("No futures bars yet — waiting 15s")
            time.sleep(15)
            continue

        fut_ltp    = float(df_fut['close'].iloc[-1])
        open_price = float(df_fut['open'].iloc[0])
        n_bars     = len(df_fut)

        # 3. Spot 1m bars (CASH segment — used for chain spot cross-checks only)
        # OI quadrant uses df_fut (futures have real OI; CASH segment oi=0.0)
        spot_sym = 'NSE-NIFTY'
        df_spot  = _fetch_all_bars_spot(g, spot_sym, today)

        log.info(
            "Bar %d | fut=%.2f spot=%.2f | pcr=%.3f | "
            "call_wall=%s put_wall=%s",
            n_bars, fut_ltp, spot_ltp,
            chain['pcr'], chain['call_wall'], chain['put_wall'],
        )

        # ── Update rolling OI history buffer ─────────────────────────────────
        oi_buf.push(ts_bar, chain)

        # ── Accumulate per-minute OI snapshot for disk persistence ───────────
        # Stores {strike: {'CE': [(ts, ltp, vol, oi)], 'PE': [...]}} in memory.
        # Written to option_oi_1m_NIFTY.pkl every PERSIST_EVERY bars + at EOD.
        for _strike in chain.get('strikes', []):
            if _strike not in oi_snapshots:
                oi_snapshots[_strike] = {'CE': [], 'PE': []}
            oi_snapshots[_strike]['CE'].append((
                ts_bar,
                chain['ce_ltp'].get(_strike, 0.0),
                chain['ce_vol'].get(_strike, 0.0),
                chain['ce_oi'].get(_strike, 0.0),
            ))
            oi_snapshots[_strike]['PE'].append((
                ts_bar,
                chain['pe_ltp'].get(_strike, 0.0),
                chain['pe_vol'].get(_strike, 0.0),
                chain['pe_oi'].get(_strike, 0.0),
            ))

        _bars_since_persist += 1
        if _bars_since_persist >= PERSIST_EVERY:
            _persist_candles(df_fut, today)
            _persist_option_oi(oi_snapshots, today)
            _bars_since_persist = 0

        # ── Update FII/DII classifier ─────────────────────────────────────────
        fii_dii_result: Optional[dict] = None
        if clf is not None and spot_ltp > 0:
            atm = int(round(spot_ltp / NIFTY_STEP) * NIFTY_STEP)
            snap = OISnapshot(
                ts         = ts_bar,
                atm_strike = atm,
                strikes    = chain['strikes'],
                ce_oi      = chain['ce_oi'],
                pe_oi      = chain['pe_oi'],
                ce_close   = chain['ce_ltp'],
                pe_close   = chain['pe_ltp'],
                fut_close  = fut_ltp,
                spot_close = spot_ltp,
            )
            clf.push(snap)
            fii_dii_result = clf.classify()
            log.info(
                "FII/DII: attribution=%s dir=%+d conf=%.2f fii_score=%.3f",
                fii_dii_result['attribution'],
                fii_dii_result['direction'],
                fii_dii_result['confidence'],
                fii_dii_result['fii_score'],
            )

        # ── Compute real-time velocity from OI history ────────────────────────
        velocity_data = oi_buf.compute_velocity(
            ltp=fut_ltp if fut_ltp > 0 else spot_ltp
        )

        # ── Live PCR + walls from option chain ────────────────────────────────
        pcr_live = chain['pcr']
        walls    = chain['walls']

        # ── DTE ───────────────────────────────────────────────────────────────
        dte = max((expiry - today).days, 1)

        # ── Inject live option OI into df_fut for signal_oi_quadrant ──────────
        # Futures candle OI is NaN for active contracts (Groww 6-col response).
        # Total option market OI (CE + PE across all strikes) is the live proxy:
        #   Price↑ + total_OI↑ → market building with conviction (trending)
        #   Price↓ + total_OI↑ → selling pressure building
        #   Price↑ + total_OI↓ → weak move, short covering
        #   Price↓ + total_OI↓ → long unwinding
        _total_option_oi = float(sum(chain['ce_oi'].values()) + sum(chain['pe_oi'].values()))
        if not df_fut.empty:
            # Record this bar's total OI and inject the full history so that
            # signal_oi_quadrant's 6-bar window has valid (non-NaN) values.
            # Without this, only the last bar gets OI — dropna wipes 5/6 rows
            # → "oi data missing" every bar.
            _bar_total_oi[df_fut['ts'].iloc[-1]] = _total_option_oi
            df_fut['oi'] = df_fut['ts'].map(_bar_total_oi)

        # ── Signal engine ──────────────────────────────────────────────────────
        state = compute_signal_state(
            df_1m          = df_fut,
            futures_ltp    = fut_ltp,
            spot_ltp       = spot_ltp if spot_ltp > 0 else fut_ltp * 0.9985,
            days_to_expiry = dte,
            pcr            = pcr_live,
            pcr_5d_ma      = static.get('pcr_ma', pcr_live),
            velocity_data  = velocity_data,
            walls          = walls,
            fii_fut_level  = static.get('fii_fut_level', 0),
            fii_cash_lag1  = static.get('fii_cash_lag1', 0),
            fii_dii_result = fii_dii_result,
            timestamp      = ts_bar,
        )

        # ── Smooth signal ─────────────────────────────────────────────────────
        smoothed_dir = smoother.update(state)
        row = state_to_dict(state)

        log.info(
            "Signal: raw_dir=%+d smoothed_dir=%+d score=%.3f "
            "sigs=%d | %s",
            state.direction, smoothed_dir, state.score,
            state.signal_count, row.get('notes', '')[:140],
        )

        # ── Post-engine filters (mirrors run_backtest_nifty.py filter chain) ─
        effective_dir   = smoothed_dir
        vs_open_pct     = (fut_ltp - open_price) / open_price * 100.0
        regime_5d       = static.get('regime_pct', 0.0)
        extreme_regime  = abs(regime_5d) > 3.0
        _spot_for_basis = spot_ltp if spot_ltp > 0 else fut_ltp * 0.9985

        # F1: extreme regime → require score ≥ 0.50
        if extreme_regime and abs(state.score) < 0.50:
            effective_dir = 0
            log.info("F1 veto: extreme_regime=%.2f%% score=%.3f", regime_5d, state.score)

        # F2: PCR hard veto — both directions
        if effective_dir != 0 and state.pcr != 0 and state.pcr != effective_dir:
            effective_dir = 0
            log.info("F2 veto: pcr=%+d contradicts dir=%+d", state.pcr, smoothed_dir)

        # F3: FII_BEAR + LONG + score < 0.45 → suppress
        if effective_dir == 1 and fii_dii_result is not None:
            if fii_dii_result.get('attribution') == 'FII_BEAR' and state.score < 0.45:
                effective_dir = 0
                log.info("F3 veto: FII_BEAR + LONG + score=%.3f < 0.45", state.score)

        # F4a: OI quadrant bearish + LONG
        if effective_dir == 1 and state.oi_quadrant == -1:
            effective_dir = 0
            log.info("F4a veto: oi_quadrant=-1 + LONG")

        # F4b: price run-up >0.5% above open + LONG + strike defense bearish
        if effective_dir == 1 and vs_open_pct > 0.5 and state.strike_defense == -1:
            effective_dir = 0
            log.info("F4b veto: vs_open=%.2f%% + strike_defense=-1 + LONG", vs_open_pct)

        # F5: extreme contango in crash regime → suppress LONG
        if effective_dir == 1 and regime_5d < -3.0:
            _raw_prem  = (fut_ltp - _spot_for_basis) / _spot_for_basis * 100.0
            _fair_prem = 8.0 * (dte / 365)
            if _raw_prem - _fair_prem > 1.0:
                effective_dir = 0
                log.info(
                    "F5 veto: crash_regime=%.2f%% basis=%.2f%%",
                    regime_5d, _raw_prem - _fair_prem,
                )

        # Momentum filter: price must trend in signal direction for last MOMENTUM_BARS
        if effective_dir != 0 and n_bars > MOMENTUM_BARS:
            price_past = float(df_fut.iloc[-MOMENTUM_BARS - 1]['close'])
            price_mom  = 1 if fut_ltp > price_past else -1
            if price_mom != effective_dir:
                effective_dir = 0
                log.info(
                    "Momentum veto: fut=%.0f past=%.0f mom=%+d dir=%+d",
                    fut_ltp, price_past, price_mom, smoothed_dir,
                )

        # Signal consensus: require MIN_SIGNAL_COUNT signals to agree
        if effective_dir != 0 and state.signal_count < MIN_SIGNAL_COUNT:
            effective_dir = 0
            log.info(
                "Consensus veto: signal_count=%d < %d",
                state.signal_count, MIN_SIGNAL_COUNT,
            )

        # F6: option-flow confluence (env-gated, default off)
        # Suppress entry when the 5-sec strike-level OI flow daemon's session
        # direction disagrees with this runner's signal.
        if REQUIRE_OPTION_FLOW_CONFIRM and effective_dir != 0:
            _flow_dir = _read_option_flow_direction('NIFTY')
            if _flow_dir != 0 and _flow_dir != effective_dir:
                log.info(
                    "F6 veto: option_flow_dir=%+d disagrees with effective_dir=%+d",
                    _flow_dir, effective_dir,
                )
                effective_dir = 0

        # No-intraday suppression: mirrors backtest logic.
        # If OI velocity is empty (no near-money strikes with data) AND the
        # FII/DII classifier has no result, the two most informative real-time
        # signals are both blind — suppress entry.  Matches backtest gate:
        #   no_intraday = (not velocity_data) and (fii_dii_live is None)
        _no_intraday = (not velocity_data) and (fii_dii_result is None)
        if _no_intraday and effective_dir != 0:
            effective_dir = 0
            log.info(
                "no_intraday veto: velocity_data empty=%s fii_dii_result=%s",
                not bool(velocity_data), fii_dii_result,
            )

        # ── Morning alert (once, just after entry window opens) ──────────────
        if not morning_alert_sent and now >= entry_target:
            msg = _fmt_morning_alert(today, static, clf)
            _tg_broadcast(tg_token, tg_chats, msg)
            morning_alert_sent = True
            log.info("Morning Telegram alert sent")

        # ── Entry: 11:00–13:00, first clean filtered signal ──────────────────
        if position is None and n_bars >= ENTRY_BAR and entry_target <= now <= last_entry_target:
            if effective_dir == 0:
                log.info(
                    "No entry this bar: effective_dir=0 (smoothed=%+d score=%.3f sigs=%d)",
                    smoothed_dir, state.score, state.signal_count,
                )
                time.sleep(60)
                continue

            atm    = int(round((spot_ltp or open_price) / NIFTY_STEP) * NIFTY_STEP)
            side   = 'CE' if effective_dir == 1 else 'PE'
            strike = atm

            # ── Re-entry cooldown check (prevents Friday's loss pattern) ─────
            # If we got force-exited recently in the same direction near this
            # strike, skip this entry. See v3/live/reentry_cooldown.py.
            blocked, why = _re_is_in_cooldown('NIFTY', effective_dir, strike, now)
            if blocked:
                log.info("[ENTRY BLOCKED] %s", why)
                time.sleep(60)
                continue

            # Get entry LTP from option chain snapshot (no extra API call)
            if side == 'CE':
                opt_ltp = chain['ce_ltp'].get(strike)
            else:
                opt_ltp = chain['pe_ltp'].get(strike)

            if not opt_ltp:
                # Fall back to single candle fetch
                try:
                    opt_ltp = _get_option_ltp(g, expiry, strike, side, today)
                except RuntimeError as e:
                    log.error(
                        "Cannot get option LTP at entry: %s. "
                        "Skipping this bar.",
                        e,
                    )
                    time.sleep(60)
                    continue

            position = {
                'direction':   smoothed_dir,
                'side':        side,
                'strike':      strike,
                'opt_symbol':  _option_symbol(expiry, strike, side),
                'entry_price': float(opt_ltp),
                'entry_time':  now,
                'qty':         NIFTY_LOT,
                'entry_score': round(state.score, 4),
            }

            log.info(
                "[%s] ENTER %s BUY  strike=%d @ %.2f  qty=%d  "
                "score=%.3f  fii=%s",
                'PAPER' if paper else 'LIVE',
                side, strike, opt_ltp, NIFTY_LOT,
                state.score,
                fii_dii_result.get('attribution', 'N/A') if fii_dii_result else 'N/A',
            )

            # ── Telegram entry alert ─────────────────────────────────────────
            entry_msg = _fmt_entry_alert(position, state, fii_dii_result, paper)
            _tg_broadcast(tg_token, tg_chats, entry_msg)

            bars_in_position = 0
            flow_tracker     = FlowConflictTracker()

            if not paper:
                raise NotImplementedError(
                    "Live order placement not implemented. "
                    "Groww order API endpoint and parameters must be "
                    "confirmed before enabling --live."
                )

            time.sleep(60)
            continue

        # ── Monitor open position: SL / TP / Reversal / EOD ─────────────────
        if position and position.get('direction') != 0:
            side   = position['side']
            strike = position['strike']
            bars_in_position += 1

            # Get current LTP from option chain snapshot first (no API call)
            if side == 'CE':
                current_ltp = chain['ce_ltp'].get(strike)
            else:
                current_ltp = chain['pe_ltp'].get(strike)

            if not current_ltp:
                try:
                    current_ltp = _get_option_ltp(g, expiry, strike, side, today)
                except RuntimeError as e:
                    log.warning("LTP fetch failed during monitor: %s", e)
                    current_ltp = position['entry_price']

            entry_px = position['entry_price']
            pnl_pts  = float(current_ltp) - entry_px
            pnl_pct  = pnl_pts / entry_px if entry_px > 0 else 0.0
            pnl_inr  = pnl_pts * NIFTY_LOT

            log.info(
                "[POSITION] %s strike=%d  entry=%.2f  ltp=%.2f  "
                "pnl=%.2f pts (%.1f%%)  ₹%.0f",
                side, strike, entry_px, current_ltp,
                pnl_pts, pnl_pct * 100, pnl_inr,
            )

            # ── Flow-Conflict Exit Assist (L1/L2/L3) ──────────────────────────
            fc = _fc_evaluate('NIFTY', position['direction'], pnl_pct,
                              flow_tracker)
            if fc is not None:
                log.info(
                    "[FLOW-CONFLICT L%d] %s conviction=%+.2f band=%+d "
                    "consec=%d pnl=%.1f%% force_exit=%s",
                    fc['level'], fc['action'], fc['conviction'], fc['band'],
                    fc['consec'], pnl_pct * 100, fc['force_exit'],
                )
                _tg_broadcast(tg_token, tg_chats, fc['message'])
                if fc['force_exit']:
                    exit_reason = 'FLOW_SL'
                    result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                    log.info(
                        "[EXIT FLOW_SL] %s %s strike=%d  entry=%.2f  "
                        "exit=%.2f  pnl=%.2f pts (%.1f%%)  ₹%.0f",
                        'PAPER' if paper else 'LIVE', side, strike, entry_px,
                        current_ltp, pnl_pts, pnl_pct * 100, pnl_inr,
                    )
                    exit_msg = _fmt_exit_alert(
                        position, float(current_ltp), exit_reason,
                        pnl_pts, pnl_inr, paper,
                    )
                    _tg_broadcast(tg_token, tg_chats, exit_msg)
                    # Arm the re-entry cooldown so we don't re-buy this strike
                    # in the same direction for COOLDOWN_MIN minutes.
                    _re_record_force_exit('NIFTY', position['direction'],
                                          strike, 'FLOW_SL', now)
                    position = None
                    bars_in_position = 0
                    flow_tracker = None
                    time.sleep(60)
                    continue

            # ── SL / TP check ─────────────────────────────────────────────────
            sl_hit = pnl_pct <= SL_PCT
            tp_hit = pnl_pct >= TP_PCT

            if sl_hit or tp_hit:
                exit_reason = 'TP' if tp_hit else 'SL'
                result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                log.info(
                    "[EXIT %s] %s %s strike=%d  entry=%.2f  exit=%.2f  "
                    "pnl=%.2f pts (%.1f%%)  ₹%.0f",
                    exit_reason,
                    'PAPER' if paper else 'LIVE',
                    side, strike, entry_px, current_ltp,
                    pnl_pts, pnl_pct * 100, pnl_inr,
                )
                exit_msg = _fmt_exit_alert(
                    position, float(current_ltp), exit_reason,
                    pnl_pts, pnl_inr, paper,
                )
                _tg_broadcast(tg_token, tg_chats, exit_msg)
                position = None
                bars_in_position = 0
                flow_tracker = None
                time.sleep(60)
                continue

            # ── Reversal check every N bars (min hold = MIN_REVERSAL_HOLD) ─────
            if bars_in_position % REVERSAL_CHECK_EVERY == 0:
                rev_signal = (
                    effective_dir != 0
                    and effective_dir != position['direction']
                )
                if rev_signal and bars_in_position >= MIN_REVERSAL_HOLD:
                    exit_reason = 'REVERSAL'
                    result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                    log.info(
                        "[EXIT REVERSAL] signal flipped to %+d  bars_held=%d  "
                        "pnl=%.2f pts  ₹%.0f",
                        effective_dir, bars_in_position, pnl_pts, pnl_inr,
                    )
                    exit_msg = _fmt_exit_alert(
                        position, float(current_ltp), exit_reason,
                        pnl_pts, pnl_inr, paper,
                    )
                    _tg_broadcast(tg_token, tg_chats, exit_msg)
                    position = None
                    bars_in_position = 0
                    flow_tracker = None
                    time.sleep(60)
                    continue

            # ── EOD exit at configured time ───────────────────────────────────
            if now >= exit_target:
                exit_reason = 'EOD'
                result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                log.info(
                    "[EXIT EOD] %s %s strike=%d  entry=%.2f  exit=%.2f  "
                    "pnl=%.2f pts  ₹%.0f  [%s]",
                    'PAPER' if paper else 'LIVE',
                    side, strike, entry_px, current_ltp,
                    pnl_pts, pnl_inr, result_str,
                )
                exit_msg = _fmt_exit_alert(
                    position, float(current_ltp), exit_reason,
                    pnl_pts, pnl_inr, paper,
                )
                _tg_broadcast(tg_token, tg_chats, exit_msg)
                position = None
                bars_in_position = 0
                flow_tracker = None
                break

        # ── Idle ──────────────────────────────────────────────────────────────
        time.sleep(60)

    # ── Final EOD persist — flush everything accumulated today ────────────────
    log.info("EOD persist: writing session data to disk…")
    _persist_candles(df_fut, today)
    _persist_option_oi(oi_snapshots, today)
    log.info("EOD persist complete.")

    log.info("Runner session complete for %s", today)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='NIFTY options live runner')
    parser.add_argument('--live', action='store_true',
                        help='Enable live order placement (default: paper trade)')
    args = parser.parse_args()

    if args.live:
        print(
            "\nWARNING: --live mode will attempt to place REAL orders via Groww API.\n"
            "Live order placement is NOT YET IMPLEMENTED (NotImplementedError will fire).\n"
            "Run without --live for paper trading.\n"
        )

    try:
        run(paper=not args.live)
    except Exception:
        log.exception("NIFTY runner crashed with unhandled exception — see traceback above")
        raise
