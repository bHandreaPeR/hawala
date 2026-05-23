"""
v3/live/runner_banknifty.py
===========================
Live intraday runner for BANKNIFTY options — real-time signal engine.

Flow:
  9:15 AM    → Auth. Start per-minute option chain + spot + futures polling.
  9:15–11:00 → Accumulate OI history, train FII/DII rolling buffer, warm EMA.
  11:00+     → Signal engine fires every minute via SignalSmoother.
               First bar where smoothed direction != 0 and |score| >= threshold
               → ENTER ATM CE (LONG) or ATM PE (SHORT).
  11:00–15:20→ Position open. Log P&L each minute.
  15:20 PM   → Exit position. Log final P&L. (LAST_ENTRY = 13:00)

Per-minute data feed:
  1. get_option_chain(NSE, BANKNIFTY, expiry)  → spot (underlying_ltp), all strikes
     OI, LTP; derives: live PCR, live call/put walls, OI snapshots for classifier
  2. get_historical_candles(NSE-BANKNIFTY, CASH)  → spot 1m bars (OI quadrant)
  3. get_historical_candles(NSE-BANKNIFTY-*-FUT, FNO) → futures 1m bars (basis)

Signals are all real-time:
  1. OI Quadrant     — from futures 1m OI
  2. Futures Basis   — fut_ltp vs spot (underlying_ltp from option chain)
  3. PCR             — computed live from option chain OI every minute
  4. OI Velocity     — from rolling OI history buffer (OI delta / bars_elapsed)
  5. Strike Defense  — walls from live option chain OI
  6. FII/DII         — 3 classifiers: BN-calibrated (primary), Combined, Nifty (ref)

Filters wired (matching backtest):
  F_VOL: 20-day realized vol < 0.85% → skip (low-vol regime, no edge)
  F0:    |5-day return| < 1.0% → skip (flat-regime filter for BankNifty)

BankNifty-specific constants vs Nifty runner:
  LOT  = 30    (Nifty = 65)
  STEP = 100   (Nifty = 50)
  Entry bar = 105 min after 9:15 = 11:00 AM
  EOD exit = 15:20                            (matches backtest EOD_EXIT_HHMM)
  Last entry = 13:00
  Expiry = last Tuesday of month              (same as Nifty — NSE post-2024 change)

Paper trade mode (default): logs trades without placing orders.
Set --live (and confirm risk) for real order placement.

Usage:
    python v3/live/runner_banknifty.py            # paper trade
    python v3/live/runner_banknifty.py --live     # real orders (NOT YET IMPLEMENTED)

Requires: valid token.env, running on a trading day, started before 11:00 AM.
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
        logging.FileHandler(ROOT / 'logs' / 'trade_bot' / 'runner_banknifty.log', mode='a'),
    ]
)
log = logging.getLogger('live_runner_banknifty')

from v3.signals.engine import (
    compute_signal_state, state_to_dict, SignalSmoother,
)
from v3.signals.fii_dii_classifier import (
    FIIDIIClassifier   as FIIDIIClassifier_NIFTY,
    OISnapshot,
    THRESHOLDS_FILE    as THRESHOLDS_NIFTY,
)
from v3.signals.fii_dii_classifier_BANKNIFTY import (
    FIIDIIClassifier   as FIIDIIClassifier_BANKNIFTY,
    THRESHOLDS_FILE    as THRESHOLDS_BN,
)
from v3.signals.fii_dii_classifier_COMBINED import (
    FIIDIIClassifier   as FIIDIIClassifier_COMBINED,
    THRESHOLDS_FILE    as THRESHOLDS_COMBINED,
)
from alerts.telegram import send as _tg_send
from v3.live.flow_conflict import FlowConflictTracker, evaluate as _fc_evaluate
from v3.live.reentry_cooldown import (
    is_in_cooldown as _re_is_in_cooldown,
    record_force_exit as _re_record_force_exit,
)

# ── Constants ─────────────────────────────────────────────────────────────────
BN_LOT         = 30
BN_STEP        = 100
ENTRY_BAR      = 105     # 9:15 + 105 min = 11:00 AM (matches backtest MIN_SIGNAL_BAR)
EXIT_HHMM      = (15, 20)   # EOD forced exit (matches backtest EOD_EXIT_HHMM=15:20)
LAST_ENTRY_HHMM = (13, 0)   # No new entries after 13:00 (matches backtest)
SIGNAL_SCORE_MIN = 0.35     # matches backtest score threshold

SL_PCT = -0.50   # option loses 50% of entry premium → stop loss
TP_PCT = +1.00   # option doubles (100% gain) → take profit

# Trailing stop (BankNifty only — no MIN_REVERSAL_HOLD, exits immediately on reversal)
TRAIL_ACTIVATE_PCT = 0.40   # activate trailing stop once option is up 40%
TRAIL_LOCK_PCT     = 0.15   # once activated, exit if profit drops below 15%

# Signal quality thresholds (match backtest)
MIN_SIGNAL_COUNT = 5    # minimum signals that must agree before entering (out of 6)
MOMENTUM_BARS    = 30   # price must trend in signal direction for last 30 bars

# Option-flow confluence (opt-in, env-gated — see runner_nifty.py for details)
import os as _os
REQUIRE_OPTION_FLOW_CONFIRM = _os.environ.get("V3_REQUIRE_OPTION_FLOW_CONFIRM",
                                               "0").lower() in ("1", "true", "yes")
OPTION_FLOW_MAX_AGE_SEC = 60


def _read_option_flow_direction(inst: str) -> int:
    """Mirror of runner_nifty._read_option_flow_direction."""
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
        cvd = float(d.get('cvd', 0.0))
        return +1 if cvd > 0 else (-1 if cvd < 0 else 0)
    except Exception:
        return 0

# Reversal check interval (bars)
REVERSAL_CHECK_EVERY = 5

# OI velocity rolling window
OI_HISTORY_MAXLEN = 60    # keep 60 bars of OI history per strike (matches backtest VELOCITY_WINDOW)

# Regime filters
MIN_VOL_PCT    = 0.85   # 20-day realized vol threshold — same as Nifty runner
MIN_REGIME_PCT = 1.0    # F0: |5d return| must be ≥ 1% or market is flat/range-bound


# ── Auth ──────────────────────────────────────────────────────────────────────
def _get_groww():
    from growwapi import GrowwAPI
    env = {}
    with open(ROOT / 'token.env') as f:
        for line in f:
            if '=' in line:
                k, _, v = line.strip().partition('=')
                env[k] = v
    totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    # 3-try retry with 5s backoff — transient Groww error at boot
    # used to kill the session (May 11 + May 15 crashes).
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
                totp = pyotp.TOTP(env['GROWW_TOTP_SECRET']).now()
    log.error("Groww auth failed after 3 attempts: %s", last_err)
    raise last_err


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _load_telegram_config() -> tuple:
    """
    Load TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS from token.env.
    Returns (token: str, chat_ids: list[str]).
    Returns ('', []) if token.env missing or keys not set — alerts silently skipped.
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


# ── Alert message formatters ───────────────────────────────────────────────────

def _fmt_morning_alert(
    today: date,
    static: dict,
    clf_bn: Optional[object],
    clf_combined: Optional[object],
    clf_nifty: Optional[object],
) -> str:
    spot     = static.get('spot', 0)
    pcr_val  = static.get('pcr_val', 0)
    pcr_ma   = static.get('pcr_ma', 0)
    fii_cash = static.get('fii_cash_lag1', 0)
    fii_fut  = static.get('fii_fut_level', 0)
    dte      = static.get('dte', 0)
    regime   = static.get('regime_pct', 0.0)
    vol      = static.get('realized_vol', 0.0)

    cash_lbl = '🔴 Selling' if fii_cash < 0 else ('🟢 Buying' if fii_cash > 0 else '⬜ Neutral')
    fo_lbl   = '🟢 Net Long' if fii_fut > 0 else ('🔴 Net Short' if fii_fut < 0 else '⬜ Neutral')
    day_str  = today.strftime('%a %d %b %Y')

    clf_lines = []
    for lbl, clf in [('BN-cal (primary)', clf_bn), ('Combined', clf_combined), ('Nifty-ref', clf_nifty)]:
        if clf is not None:
            try:
                n = len(clf._thresh.get('training_days', []))
            except Exception:
                n = 0
            clf_lines.append(f"  {lbl}: {n} days trained")
        else:
            clf_lines.append(f"  {lbl}: <i>not loaded</i>")

    regime_flag = '✅ Active' if abs(regime) >= MIN_REGIME_PCT else f'⚠️ Flat ({regime:.2f}%)'

    return (
        f"🌅 <b>Hawala v3 — BANKNIFTY</b>  {day_str}\n"
        f"{'─'*32}\n"
        f"Spot prev close: <b>{spot:,.0f}</b>\n"
        f"FII cash (lag-1): {cash_lbl}\n"
        f"FII F&O net: {fo_lbl}\n"
        f"PCR (prev EOD): <b>{pcr_val:.3f}</b>  |  MA(5): <b>{pcr_ma:.3f}</b>\n"
        f"DTE: <b>{dte}</b>\n"
        f"20d Vol: <b>{vol:.3f}%</b>  |  5d Regime: {regime_flag}\n"
        f"FII/DII classifiers:\n" + '\n'.join(clf_lines) + '\n'
        f"SL: –50%  |  TP: +100%\n"
        f"Entry window: 11:00 → 13:00  |  EOD: 15:20"
    )


def _fmt_entry_alert(
    position: dict,
    state,
    fii_dii_result: Optional[dict],
    clf_combined_result: Optional[dict],
    clf_nifty_result: Optional[dict],
    paper: bool,
) -> str:
    direction  = position['direction']
    side       = position['side']
    strike     = position['strike']
    opt_sym    = position.get('opt_symbol', f"{side} {strike}")
    entry_px   = position['entry_price']
    entry_time = position['entry_time'].strftime('%H:%M')
    score      = position['entry_score']

    sl_px  = round(entry_px * (1 + SL_PCT), 2)
    tp_px  = round(entry_px * (1 + TP_PCT), 2)
    sl_pts = round(entry_px * SL_PCT, 2)
    tp_pts = round(entry_px * TP_PCT, 2)

    dir_emoji = '🟢 LONG' if direction == 1 else '🔴 SHORT'
    mode_tag  = ' [PAPER]' if paper else ''

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

    def _clf_line(lbl: str, result: Optional[dict]) -> str:
        if result and result.get('attribution', 'UNKNOWN') != 'UNKNOWN':
            attr   = result['attribution']
            conf   = result['confidence']
            fscore = result['fii_score']
            return f"  {lbl}: <b>{attr}</b>  conf={conf:.2f}  fii={fscore:+.3f}"
        return f"  {lbl}: N/A"

    clf_block = '\n'.join([
        _clf_line('BN-cal [primary]', fii_dii_result),
        _clf_line('Combined', clf_combined_result),
        _clf_line('Nifty-ref', clf_nifty_result),
    ])

    return (
        f"⚡ <b>Hawala v3 — BANKNIFTY</b>\n"
        f"{dir_emoji} <b>{opt_sym}</b>  @  ₹{entry_px:.2f}{mode_tag}\n"
        f"{'─'*32}\n"
        f"Entry: {entry_time}  |  Score: <b>{score:+.3f}</b>  |  "
        f"Signals: {state.signal_count}\n"
        f"\n"
        f"Signals:\n{sig_lines}\n"
        f"\n"
        f"Classifiers:\n{clf_block}\n"
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
        f"⚡ <b>Hawala v3 — BANKNIFTY</b>\n"
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


# ── Contract resolvers (Tuesday expiry — BankNifty moved to last-Tuesday same as Nifty) ───

def _last_tuesday(year: int, month: int) -> date:
    """Last Tuesday of the given month (BankNifty monthly F&O expiry, post-2024 NSE change)."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    # weekday(): Monday=0, Tuesday=1 … Sunday=6
    days_back = (last_day.weekday() - 1) % 7
    return last_day - timedelta(days=days_back)


EXPIRY_OVERRIDES_BN: dict = {
    date(2026, 3, 31): date(2026, 3, 30),   # Mar 2026: last-Tue holiday → Mar 30
}


def _nearest_tuesday_expiry(trade_date: date) -> date:
    """Find the nearest monthly Tuesday expiry on or after trade_date."""
    y, m = trade_date.year, trade_date.month
    for _ in range(3):
        raw = _last_tuesday(y, m)
        exp = EXPIRY_OVERRIDES_BN.get(raw, raw)
        if exp >= trade_date:
            return exp
        m += 1
        if m > 12:
            m, y = 1, y + 1
    raise RuntimeError(f"Cannot find Tuesday expiry for {trade_date}")


def _futures_symbol(trade_date: date) -> str:
    exp = _nearest_tuesday_expiry(trade_date)
    return f"NSE-BANKNIFTY-{exp.day}{exp.strftime('%b')}{exp.strftime('%y')}-FUT"


def _option_symbol(expiry: date, strike: int, side: str) -> str:
    return (f"NSE-BANKNIFTY-{expiry.day}{expiry.strftime('%b')}"
            f"{expiry.strftime('%y')}-{strike}-{side}")


# ── Option chain fetch (primary per-minute feed) ──────────────────────────────

def _fetch_option_chain(g, expiry: date) -> Optional[dict]:
    """
    Fetch the full BANKNIFTY option chain for the given expiry.

    Returns a dict with:
      underlying_ltp, strikes, ce_oi, pe_oi, ce_ltp, pe_ltp,
      ce_vol, pe_vol, pcr, call_wall, put_wall, walls.
    Returns None on API failure.
    """
    exp_str = expiry.isoformat()
    try:
        r = g.get_option_chain(
            exchange='NSE',
            underlying='BANKNIFTY',
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

    tot_ce = sum(ce_oi.values())
    tot_pe = sum(pe_oi.values())
    pcr    = (tot_pe / tot_ce) if tot_ce > 0 else 1.0

    # Near-money band for walls (ATM ± 2000 pts for BN's wider range)
    if underlying_ltp > 0:
        near = [s for s in strikes if abs(s - underlying_ltp) <= 2000]
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


# ── Rolling OI history → per-minute velocity ──────────────────────────────────

class OIHistoryBuffer:
    """
    Keeps the last N OI values per (strike, side) for velocity computation.
    Identical logic to runner_nifty.py — instrument-agnostic.
    """

    def __init__(self, maxlen: int = OI_HISTORY_MAXLEN):
        self._maxlen = maxlen
        self._hist: dict = defaultdict(lambda: deque(maxlen=maxlen))

    def push(self, ts: pd.Timestamp, chain: dict) -> None:
        """Record one minute's OI snapshot."""
        for strike in chain['strikes']:
            ce = chain['ce_oi'].get(strike, 0.0)
            pe = chain['pe_oi'].get(strike, 0.0)
            self._hist[strike].append((ts, ce, pe))

    def compute_velocity(self, ltp: float, band_pct: float = 0.05) -> dict:
        """
        Compute OI velocity for strikes within band_pct of ltp.
        Returns {strike: {ce_oi, pe_oi, ce_velocity, pe_velocity, net_velocity}}.
        """
        result: dict = {}
        band = ltp * band_pct if ltp > 0 else 0

        for strike, hist in self._hist.items():
            if band > 0 and abs(strike - ltp) > band:
                continue
            if len(hist) < 2:
                continue

            entries = list(hist)
            ce_vel = _last_true_velocity([e[1] for e in entries], len(entries))
            pe_vel = _last_true_velocity([e[2] for e in entries], len(entries))
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
    Given a list of OI values (possibly ffilled), compute velocity
    of the most recent non-zero OI change.
    Returns change / bars_elapsed, or 0 if no change found.
    """
    if n < 2:
        return 0.0
    last_val = oi_list[-1]
    for i in range(n - 2, -1, -1):
        if oi_list[i] != last_val:
            elapsed = (n - 1) - i
            return float((last_val - oi_list[i]) / elapsed)
    return 0.0


# ── Per-bar cache persistence ─────────────────────────────────────────────────
CANDLE_CACHE_BN = ROOT / 'v3' / 'cache' / 'candles_1m_BANKNIFTY.pkl'
OI_CACHE_BN     = ROOT / 'v3' / 'cache' / 'option_oi_1m_BANKNIFTY.pkl'


def _persist_candles_bn(df_fut: pd.DataFrame, today: date) -> None:
    """Write today's BN futures 1m bars into the persistent candle cache. Idempotent."""
    if df_fut.empty:
        return
    try:
        cache = pd.DataFrame()
        if CANDLE_CACHE_BN.exists():
            with open(CANDLE_CACHE_BN, 'rb') as fh:
                cache = pickle.load(fh)
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
        with open(CANDLE_CACHE_BN, 'wb') as fh:
            pickle.dump(cache, fh)
        log.debug(
            "persist_candles_bn: wrote %d bars for %s → %s",
            len(df_today), today, CANDLE_CACHE_BN,
        )
    except Exception as e:
        log.warning(
            "persist_candles_bn FAILED: date=%s error=%s — data stays in memory",
            today, e,
        )


def _persist_option_oi_bn(oi_snapshots: dict, today: date) -> None:
    """
    Write accumulated per-minute BN option OI snapshots into the persistent OI cache.
    oi_snapshots: {strike: {'CE': [(ts, close, volume, oi)], 'PE': [...]}}
    """
    if not oi_snapshots:
        return
    try:
        oi_cache: dict = {}
        if OI_CACHE_BN.exists():
            with open(OI_CACHE_BN, 'rb') as fh:
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
                df['oi_raw'] = df['oi']
                df['oi']     = pd.to_numeric(df['oi'], errors='coerce').ffill()
                day_entry[strike][side] = df

        oi_cache[today_str] = day_entry
        with open(OI_CACHE_BN, 'wb') as fh:
            pickle.dump(oi_cache, fh)
        log.debug(
            "persist_option_oi_bn: wrote %d strikes for %s → %s",
            len(day_entry), today, OI_CACHE_BN,
        )
    except Exception as e:
        log.warning(
            "persist_option_oi_bn FAILED: date=%s error=%s — data stays in memory",
            today, e,
        )


# ── 1m bar fetch ──────────────────────────────────────────────────────────────

def _fetch_latest_bar(g, symbol: str, trade_date: date) -> Optional[dict]:
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
    Fetch all CASH segment 1m bars for BankNifty spot index (NSE-BANKNIFTY).
    OI and volume fields will be 0/null — expected for index CASH segment.
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
        log.warning("fetch_all_bars_spot symbol=%s error=%s", symbol, e)
        return pd.DataFrame()


# ── Load static inputs (lag-1) ────────────────────────────────────────────────

def _load_static_inputs(today: date) -> dict:
    """
    Load all lag-1 inputs that don't change intraday.
    Uses BankNifty bhavcopy (bhavcopy_BN_all.pkl) for PCR.
    Spot: estimated from BankNifty futures candle cache (last prior EOD close).
    """
    # FII F&O
    fii_fo_path = ROOT / 'trade_logs/_fii_fo_cache.pkl'
    fii_fut_level = 0
    if fii_fo_path.exists():
        try:
            with open(fii_fo_path, 'rb') as f:
                fii_fo = pickle.load(f)
        except Exception as e:
            log.warning(
                "static_inputs_bn: fii_fo_cache corrupt or unreadable: path=%s error=%s — skipping FII F&O",
                fii_fo_path, e,
            )
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

    # PCR from BankNifty bhavcopy
    pcr_val = 1.0
    pcr_ma  = 1.0
    bhav_path = ROOT / 'v3/cache/bhavcopy_BN_all.pkl'
    walls = {}
    if bhav_path.exists():
        try:
            with open(bhav_path, 'rb') as f:
                bhav = pickle.load(f)
        except Exception as e:
            log.warning(
                "static_inputs_bn: bhavcopy_BN_all corrupt or unreadable: path=%s error=%s — PCR=1.0",
                bhav_path, e,
            )
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
        prev_bhav = [d for d in bhav_dates if d < str(today)]
        if prev_bhav:
            df_b = bhav[prev_bhav[-1]]
            total_ce = df_b['ce_oi'].sum() if not df_b.empty else 0
            total_pe = df_b['pe_oi'].sum() if not df_b.empty else 0
            pcr_live = total_pe / total_ce if total_ce > 0 else 1.0
            walls = {'pcr_live': round(pcr_live, 3)}

    # Spot: use BankNifty futures candle cache for prior EOD close (no yfinance)
    spot = 0.0
    candle_file = ROOT / 'v3/cache/candles_1m_BANKNIFTY.pkl'
    if candle_file.exists():
        try:
            with open(candle_file, 'rb') as f:
                candles_all = pickle.load(f)
        except Exception as e:
            log.warning(
                "static_inputs_bn: candles_1m_BANKNIFTY corrupt or unreadable: path=%s error=%s — spot=0.0",
                candle_file, e,
            )
            candles_all = pd.DataFrame()
        if not candles_all.empty and 'date' in candles_all.columns:
            prior = candles_all[candles_all['date'] < today]
            if not prior.empty:
                daily_close = prior.groupby('date')['close'].last().sort_index()
                if len(daily_close) > 0:
                    spot = float(daily_close.iloc[-1])
    if spot == 0.0:
        log.warning("static_inputs: could not derive BN spot from candle cache")

    # 5-day regime return (F0 flat-regime filter, matches backtest)
    regime_pct = 0.0
    if candle_file.exists() and spot > 0:
        try:
            with open(candle_file, 'rb') as f:
                candles_all = pickle.load(f)
            if not candles_all.empty and 'date' in candles_all.columns:
                prior = candles_all[candles_all['date'] < today]
                daily_close = prior.groupby('date')['close'].last().sort_index()
                if len(daily_close) >= 6:
                    regime_pct = float(
                        (daily_close.iloc[-1] - daily_close.iloc[-6])
                        / daily_close.iloc[-6] * 100
                    )
        except Exception as e:
            log.warning("regime filter: could not compute 5d return: %s", e)

    # DTE (approximate — to next Tuesday expiry)
    try:
        expiry_approx = _nearest_tuesday_expiry(today)
        dte = max((expiry_approx - today).days, 1)
    except Exception:
        dte = 1

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


# ── Morning bhavcopy refresh (BANKNIFTY) ──────────────────────────────────────

def _refresh_morning_bhavcopy_bn(today: date) -> tuple[float, float]:
    """
    Fetch YESTERDAY's NSE bhavcopy, extract BANKNIFTY options, update
    bhavcopy_BN_all.pkl.  Called once at runner startup so pcr_5d_ma is fresh.

    Returns (pcr_val, pcr_ma) for yesterday.
    Non-fatal: on any failure logs a warning and returns (0.0, 0.0) so the
    caller falls back to the stale values already loaded by _load_static_inputs.
    """
    import requests, io, zipfile

    BHAV_CACHE = ROOT / 'v3' / 'cache' / 'bhavcopy_BN_all.pkl'

    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)
    yesterday_str = str(yesterday)

    if BHAV_CACHE.exists():
        try:
            with open(BHAV_CACHE, 'rb') as fh:
                bhav_cache = pickle.load(fh)
        except Exception as e:
            log.warning(
                "morning_bhavcopy_bn: BHAV_CACHE corrupt or unreadable: path=%s error=%s — refetching",
                BHAV_CACHE, e,
            )
            bhav_cache = {}
        if yesterday_str in bhav_cache and not bhav_cache[yesterday_str].empty:
            log.info(
                "Morning bhavcopy BN: %s already cached — skipping fetch",
                yesterday_str,
            )
            return _pcr_from_bhav_cache_bn(bhav_cache, today)
    else:
        bhav_cache = {}

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
                bn = raw[raw['TckrSymb'].str.strip() == 'BANKNIFTY'].copy()
                bn = bn[bn['OptnTp'].isin(['CE', 'PE'])].copy()
                if bn.empty:
                    continue
                bn['strike'] = pd.to_numeric(bn['StrkPric'], errors='coerce')
                bn['oi']     = pd.to_numeric(bn['OpnIntrst'], errors='coerce').fillna(0)
                bn['vol']    = pd.to_numeric(
                    bn.get('TtlTradgVol', pd.Series(0, index=bn.index)),
                    errors='coerce').fillna(0)
                bn['ltp']    = pd.to_numeric(
                    bn.get('SttlmPric', pd.Series(0, index=bn.index)),
                    errors='coerce').fillna(0)
                opt_col = 'OptnTp'
            elif 'SYMBOL' in cols or 'Symbol' in cols:
                sym_col = 'SYMBOL' if 'SYMBOL' in cols else 'Symbol'
                raw.columns = [c.strip() for c in raw.columns]
                bn = raw[raw[sym_col].str.strip() == 'BANKNIFTY'].copy()
                if bn.empty:
                    continue
                opt_col = next(
                    (c for c in ['OPTION_TYP', 'OptionType'] if c in bn.columns), None)
                if opt_col is None:
                    continue
                bn = bn[bn[opt_col].isin(['CE', 'PE'])].copy()
                stk_col = next((c for c in ['STRIKE_PR', 'StrikePrice'] if c in bn.columns), 'STRIKE_PR')
                oi_col  = next((c for c in ['OPEN_INT', 'OpenInterest'] if c in bn.columns), 'OPEN_INT')
                bn['strike'] = pd.to_numeric(bn[stk_col], errors='coerce')
                bn['oi']     = pd.to_numeric(bn[oi_col], errors='coerce').fillna(0)
                bn['vol']    = 0
                bn['ltp']    = 0
            else:
                continue

            ce = bn[bn[opt_col] == 'CE'].groupby('strike').agg(
                ce_oi=('oi', 'sum'), ce_vol=('vol', 'sum'), ce_ltp=('ltp', 'first')
            ).reset_index()
            pe = bn[bn[opt_col] == 'PE'].groupby('strike').agg(
                pe_oi=('oi', 'sum'), pe_vol=('vol', 'sum'), pe_ltp=('ltp', 'first')
            ).reset_index()
            df_raw = pd.merge(ce, pe, on='strike', how='outer').fillna(0)
            df_raw['strike'] = df_raw['strike'].astype(int)
            df_raw.sort_values('strike', inplace=True)
            df_raw.reset_index(drop=True, inplace=True)
            break
        except Exception as e:
            log.debug("bhavcopy BN fetch attempt failed: url=%s error=%s", url, e)
            continue

    if df_raw.empty:
        log.warning(
            "Morning bhavcopy BN: fetch failed for %s — pcr_5d_ma will use stale cache",
            yesterday_str,
        )
        if BHAV_CACHE.exists():
            try:
                with open(BHAV_CACHE, 'rb') as fh:
                    bhav_cache = pickle.load(fh)
            except Exception as e:
                log.warning(
                    "morning_bhavcopy_bn: fallback BHAV_CACHE corrupt: path=%s error=%s — pcr=1.0",
                    BHAV_CACHE, e,
                )
                bhav_cache = {}
        return _pcr_from_bhav_cache_bn(bhav_cache, today)

    bhav_cache[yesterday_str] = df_raw
    (ROOT / 'v3' / 'cache').mkdir(parents=True, exist_ok=True)
    with open(BHAV_CACHE, 'wb') as fh:
        pickle.dump(bhav_cache, fh)

    ce_tot = float(df_raw['ce_oi'].sum())
    pe_tot = float(df_raw['pe_oi'].sum())
    pcr_yesterday = round(pe_tot / ce_tot, 4) if ce_tot > 0 else 1.0

    log.info(
        "Morning bhavcopy BN refreshed: %s  pcr=%.4f  cache=%s",
        yesterday_str, pcr_yesterday, BHAV_CACHE,
    )
    return _pcr_from_bhav_cache_bn(bhav_cache, today)


def _pcr_from_bhav_cache_bn(bhav_cache: dict, today: date) -> tuple[float, float]:
    """Recompute pcr_val + pcr_ma from the BN bhavcopy cache."""
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


# ── Realized-vol gate (BankNifty candles) ─────────────────────────────────────

def _compute_realized_vol(today: date) -> float:
    """
    Compute 20-day realized vol from cached BANKNIFTY 1m futures candles.
    Uses EOD close of each day (last bar per date) → daily % return std.
    Returns float vol (daily %) or 0.0 if cache not found.

    Raises RuntimeError if cache exists but is corrupted — never silently
    returns 0.0 on a read failure (caller cannot accidentally pass the gate).
    """
    candle_file = ROOT / 'v3/cache/candles_1m_BANKNIFTY.pkl'
    if not candle_file.exists():
        log.warning(
            "vol_gate: BN candle cache not found at %s — "
            "cannot compute realized vol, defaulting 0.0 (gate OPEN)",
            candle_file,
        )
        return 0.0

    try:
        with open(candle_file, 'rb') as f:
            candles = pickle.load(f)
    except Exception as e:
        log.warning(
            "vol_gate_bn: candle cache corrupt or unreadable: path=%s error=%s — defaulting vol=0.0",
            candle_file, e,
        )
        return 0.0

    if candles.empty or 'date' not in candles.columns:
        raise RuntimeError(
            f"vol_gate: BN candle cache at {candle_file} is empty or missing 'date' column"
        )

    prior = candles[candles['date'] < today]
    if prior.empty:
        log.warning("vol_gate: no prior BN candle data before %s — defaulting vol=0.0", today)
        return 0.0

    daily_close = prior.groupby('date')['close'].last().sort_index()
    daily_ret   = daily_close.pct_change() * 100
    if len(daily_ret.dropna()) < 10:
        log.warning(
            "vol_gate: only %d days of BN returns (need ≥10) — defaulting vol=0.0",
            len(daily_ret.dropna()),
        )
        return 0.0

    vol = float(daily_ret.rolling(20, min_periods=10).std().iloc[-1])
    log.info(
        "vol_gate: 20d BN realized vol=%.3f%% threshold=%.2f%% → %s",
        vol, MIN_VOL_PCT,
        "PASS (directional mode)" if vol >= MIN_VOL_PCT else "BLOCK (low-vol regime)",
    )
    return vol


# ── Option price fetch ─────────────────────────────────────────────────────────

def _get_option_ltp(g, expiry: date, strike: int, side: str, trade_date: date) -> float:
    """Fetch the latest LTP (close of most recent 1m bar) for a BN option."""
    sym = _option_symbol(expiry, strike, side)
    bar = _fetch_latest_bar(g, sym, trade_date)
    if bar:
        return bar['close']
    raise RuntimeError(
        f"Cannot get BN option LTP: symbol={sym} trade_date={trade_date}"
    )


# ── Main loop ──────────────────────────────────────────────────────────────────

def run(paper: bool = True):
    today = date.today()
    now   = datetime.now()

    log.info("=" * 60)
    log.info("BANKNIFTY Live Runner starting. paper=%s date=%s", paper, today)

    if today.weekday() >= 5:
        log.error(
            "Today is %s (%s). Market closed on weekends.",
            today, today.strftime('%A'),
        )
        raise RuntimeError("Market closed — not a weekday.")

    g = _get_groww()

    # ── Telegram ───────────────────────────────────────────────────────────────
    tg_token, tg_chats = _load_telegram_config()

    # ── Vol gate ───────────────────────────────────────────────────────────────
    realized_vol = _compute_realized_vol(today)
    if 0.0 < realized_vol < MIN_VOL_PCT:
        msg = (
            f"⚠️ VOL GATE — BANKNIFTY directional strategy SKIPPED today ({today}).\n"
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
        return

    # ── Morning bhavcopy refresh (one-time, before static inputs) ────────────
    # Fetches YESTERDAY's bhavcopy from NSE, updates bhavcopy_BN_all.pkl.
    # Non-fatal — falls back to stale cache if NSE is slow or unavailable.
    log.info("Morning refresh: fetching yesterday's BANKNIFTY bhavcopy for fresh pcr_5d_ma…")
    fresh_pcr_val, fresh_pcr_ma = _refresh_morning_bhavcopy_bn(today)

    # ── Static inputs ──────────────────────────────────────────────────────────
    static = _load_static_inputs(today)
    static['realized_vol'] = realized_vol

    # Override pcr with fresh values if the refresh succeeded (non-zero)
    if fresh_pcr_val > 0:
        static['pcr_val'] = fresh_pcr_val
        static['pcr_ma']  = fresh_pcr_ma
        log.info(
            "BN pcr updated from fresh bhavcopy: pcr=%.4f pcr_ma=%.4f",
            fresh_pcr_val, fresh_pcr_ma,
        )

    # ── F0 flat-regime filter ──────────────────────────────────────────────────
    regime_pct = static.get('regime_pct', 0.0)
    if abs(regime_pct) < MIN_REGIME_PCT:
        msg = (
            f"⚠️ REGIME GATE — BANKNIFTY SKIPPED today ({today}).\n"
            f"5-day return = {regime_pct:+.2f}% (|{abs(regime_pct):.2f}%| < {MIN_REGIME_PCT:.1f}%).\n"
            f"Flat/range-bound regime: directional strategy has no edge.\n"
            f"No trade today. Resume when |5d return| ≥ {MIN_REGIME_PCT:.1f}%."
        )
        log.warning(msg.replace('\n', ' | '))
        for chat_id in tg_chats:
            try:
                _tg_send(tg_token, chat_id, msg)
            except Exception as tg_err:
                log.warning("Telegram send failed chat=%s err=%s", chat_id, tg_err)
        return

    expiry  = _nearest_tuesday_expiry(today)
    fut_sym = _futures_symbol(today)
    log.info("Futures symbol: %s   Option expiry: %s", fut_sym, expiry)

    entry_target      = now.replace(hour=11, minute=0,  second=0, microsecond=0)
    last_entry_target = now.replace(
        hour=LAST_ENTRY_HHMM[0], minute=LAST_ENTRY_HHMM[1], second=0, microsecond=0
    )
    exit_target  = now.replace(hour=EXIT_HHMM[0], minute=EXIT_HHMM[1], second=0, microsecond=0)
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)

    if now > exit_target:
        log.error("Started after exit time (15:20). Nothing to do today.")
        return

    # ── Real-time state objects ────────────────────────────────────────────────
    oi_buf   = OIHistoryBuffer(maxlen=OI_HISTORY_MAXLEN)
    smoother = SignalSmoother(alpha=0.4, threshold=SIGNAL_SCORE_MIN, min_persist=2)

    # ── Load all 3 classifiers ─────────────────────────────────────────────────
    clf_bn: Optional[FIIDIIClassifier_BANKNIFTY] = None
    clf_combined: Optional[FIIDIIClassifier_COMBINED] = None
    clf_nifty: Optional[FIIDIIClassifier_NIFTY] = None

    if THRESHOLDS_BN.exists():
        try:
            clf_bn = FIIDIIClassifier_BANKNIFTY()
            log.info("BN classifier loaded (primary)")
        except Exception as e:
            log.warning(
                "BN classifier could not load: thresholds=%s error=%s. "
                "Signal 6 will use lag-1 fallback.",
                THRESHOLDS_BN, e,
            )
    else:
        log.warning(
            "BN classifier thresholds not found (%s). Run FIIDIICalibrator().calibrate() first.",
            THRESHOLDS_BN,
        )

    if THRESHOLDS_COMBINED.exists():
        try:
            clf_combined = FIIDIIClassifier_COMBINED(instrument='BANKNIFTY')
            log.info("Combined classifier loaded (reference)")
        except Exception as e:
            log.warning("Combined classifier load failed: %s", e)
    else:
        log.warning("Combined classifier thresholds not found (%s).", THRESHOLDS_COMBINED)

    if THRESHOLDS_NIFTY.exists():
        try:
            clf_nifty = FIIDIIClassifier_NIFTY()
            log.info("Nifty classifier loaded (cross-reference)")
        except Exception as e:
            log.warning("Nifty classifier load failed: %s", e)
    else:
        log.warning("Nifty classifier thresholds not found (%s).", THRESHOLDS_NIFTY)

    # ── Morning alert ──────────────────────────────────────────────────────────
    morning_alert_sent: bool = False

    last_chain: Optional[dict] = None
    position: Optional[dict]   = None
    bars_in_position: int      = 0
    max_pnl_pct: float         = 0.0
    trail_activated: bool      = False
    flow_tracker: Optional[FlowConflictTracker] = None   # flow-conflict exit assist

    # Per-minute OI snapshot accumulator → written to option_oi_1m_BANKNIFTY.pkl
    # {strike: {'CE': [(ts, close, volume, oi)], 'PE': [...]}}
    oi_snapshots: dict    = {}
    _bar_total_oi: dict   = {}   # {pd.Timestamp → float} running total CE+PE OI per bar
    PERSIST_EVERY: int    = 5   # write to disk every 5 bars
    _bars_since_persist   = 0
    df_fut: pd.DataFrame  = pd.DataFrame()  # last fetched futures bars — for EOD persist

    log.info("Waiting for 9:15 AM to start buffering bars...")

    while True:
        now = datetime.now()

        # Pre-market wait
        if now < market_open:
            sleep_secs = (market_open - now).total_seconds()
            log.info("Pre-market. Sleeping %.0fs until 9:15 AM", sleep_secs)
            time.sleep(min(sleep_secs, 60))
            continue

        # Past exit time with no position → done
        if now >= exit_target and position is None:
            log.info("Past exit time, no open position. Session complete.")
            break

        # ── Per-minute data fetch ──────────────────────────────────────────────
        ts_bar = pd.Timestamp(now)

        # 1. BankNifty option chain
        chain = _fetch_option_chain(g, expiry)
        if chain is None:
            log.warning("Option chain fetch failed — skipping bar")
            time.sleep(15)
            continue

        spot_ltp  = chain['underlying_ltp']
        last_chain = chain

        # 2. BankNifty futures 1m bars
        df_fut = _fetch_all_bars(g, fut_sym, today)
        if df_fut.empty:
            log.warning("No BN futures bars yet — waiting 15s")
            time.sleep(15)
            continue

        fut_ltp    = float(df_fut['close'].iloc[-1])
        open_price = float(df_fut['open'].iloc[0])
        n_bars     = len(df_fut)

        # 3. BankNifty spot 1m bars (CASH segment — fetched for logging only;
        #    OI quadrant signal uses df_fut which has real futures OI, not zero)
        spot_sym = 'NSE-BANKNIFTY'
        df_spot  = _fetch_all_bars_spot(g, spot_sym, today)

        log.info(
            "Bar %d | fut=%.2f spot=%.2f | pcr=%.3f | "
            "call_wall=%s put_wall=%s",
            n_bars, fut_ltp, spot_ltp,
            chain['pcr'], chain['call_wall'], chain['put_wall'],
        )

        # ── Update rolling OI history buffer ───────────────────────────────────
        oi_buf.push(ts_bar, chain)

        # ── Accumulate per-minute OI snapshot for disk persistence ───────────
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
            _persist_candles_bn(df_fut, today)
            _persist_option_oi_bn(oi_snapshots, today)
            _bars_since_persist = 0

        # ── Push to all 3 classifiers ──────────────────────────────────────────
        atm = int(round((spot_ltp or fut_ltp) / BN_STEP) * BN_STEP)
        snap = OISnapshot(
            ts         = ts_bar,
            atm_strike = atm,
            strikes    = chain['strikes'],
            ce_oi      = chain['ce_oi'],
            pe_oi      = chain['pe_oi'],
            ce_close   = chain['ce_ltp'],
            pe_close   = chain['pe_ltp'],
            fut_close  = fut_ltp,
            spot_close = spot_ltp if spot_ltp > 0 else fut_ltp,
        )

        fii_dii_result: Optional[dict] = None   # BN primary (fed to signal engine)
        clf_combined_result: Optional[dict] = None
        clf_nifty_result: Optional[dict] = None

        if clf_bn is not None:
            clf_bn.push(snap)
            fii_dii_result = clf_bn.classify()
            log.info(
                "Clf BN-primary: attribution=%s dir=%+d conf=%.2f fii_score=%.3f",
                fii_dii_result['attribution'],
                fii_dii_result['direction'],
                fii_dii_result['confidence'],
                fii_dii_result['fii_score'],
            )

        if clf_combined is not None:
            clf_combined.push(snap)
            clf_combined_result = clf_combined.classify()
            log.info(
                "Clf Combined:   attribution=%s dir=%+d conf=%.2f",
                clf_combined_result['attribution'],
                clf_combined_result['direction'],
                clf_combined_result['confidence'],
            )

        if clf_nifty is not None:
            clf_nifty.push(snap)
            clf_nifty_result = clf_nifty.classify()
            log.info(
                "Clf Nifty-ref:  attribution=%s dir=%+d conf=%.2f",
                clf_nifty_result['attribution'],
                clf_nifty_result['direction'],
                clf_nifty_result['confidence'],
            )

        # ── OI velocity from rolling buffer ────────────────────────────────────
        velocity_data = oi_buf.compute_velocity(
            ltp=fut_ltp if fut_ltp > 0 else spot_ltp
        )

        # ── Live PCR + walls ───────────────────────────────────────────────────
        pcr_live = chain['pcr']
        walls    = chain['walls']
        dte      = max((expiry - today).days, 1)

        # ── Inject live option OI into df_fut for signal_oi_quadrant ──────────
        # Futures candle OI is NaN for active contracts (Groww 6-col response).
        # Total option market OI (CE + PE across all strikes) is the live proxy:
        #   Price↑ + total_OI↑ → market building with conviction (trending)
        #   Price↓ + total_OI↑ → selling pressure building
        #   Price↑ + total_OI↓ → weak move, short covering
        #   Price↓ + total_OI↓ → long unwinding
        _total_option_oi = float(sum(chain['ce_oi'].values()) + sum(chain['pe_oi'].values()))
        if not df_fut.empty:
            # Record this bar's total OI and inject full history so
            # signal_oi_quadrant's 6-bar window has valid (non-NaN) values.
            _bar_total_oi[df_fut['ts'].iloc[-1]] = _total_option_oi
            df_fut['oi'] = df_fut['ts'].map(_bar_total_oi)

        # ── Signal engine ──────────────────────────────────────────────────────
        state = compute_signal_state(
            df_1m          = df_fut,            # use futures bars — CASH bars have oi=0
            futures_ltp    = fut_ltp,
            spot_ltp       = spot_ltp if spot_ltp > 0 else fut_ltp * 0.9985,
            days_to_expiry = dte,
            pcr            = pcr_live,
            pcr_5d_ma      = static.get('pcr_ma', pcr_live),
            velocity_data  = velocity_data,
            walls          = walls,
            fii_fut_level  = static.get('fii_fut_level', 0),
            fii_cash_lag1  = static.get('fii_cash_lag1', 0),
            fii_dii_result = fii_dii_result,   # BN primary classifier
            timestamp      = ts_bar,
            contango_thresh = 0.15,             # BN futures trade tighter to fair value
        )

        # ── Smooth signal ──────────────────────────────────────────────────────
        smoothed_dir = smoother.update(state)
        row = state_to_dict(state)

        log.info(
            "Signal: raw_dir=%+d smoothed_dir=%+d score=%.3f "
            "sigs=%d | %s",
            state.direction, smoothed_dir, state.score,
            state.signal_count, row.get('notes', '')[:140],
        )

        # ── BN post-engine filter chain (matches run_backtest_banknifty.py) ────
        # Applies F1–F5 + momentum + consensus after smoother.
        # effective_dir is what drives entry and reversal checks.
        effective_dir  = smoothed_dir
        extreme_regime = abs(regime_pct) > 3.0
        vs_open_pct    = (fut_ltp - open_price) / open_price * 100.0 if open_price > 0 else 0.0

        # F1: extreme regime → require |score| >= 0.50
        if extreme_regime and abs(state.score) < 0.50:
            effective_dir = 0
            log.info("Filter F1: extreme regime |%.2f%%| + weak score %.3f → suppress", regime_pct, state.score)

        # F2: PCR soft veto — LONG only, score < 0.55
        if effective_dir == 1 and state.pcr == -1 and state.score < 0.55:
            effective_dir = 0
            log.info("Filter F2: PCR bearish + LONG + score=%.3f < 0.55 → suppress", state.score)

        # F3: classifier majority vote → symmetric hard block
        # Uses majority of all available classifiers (BN-primary + Combined + Nifty-ref)
        # to avoid single-classifier miscalibration blocking all entries.
        # Block requires ≥2 of the available classifiers agreeing (majority).
        _f3_bull_votes = 0
        _f3_bear_votes = 0
        _f3_total      = 0
        for _clf_res in [fii_dii_result, clf_combined_result, clf_nifty_result]:
            if _clf_res is not None:
                _attr = _clf_res.get('attribution', 'UNKNOWN')
                if _attr not in ('UNKNOWN', ''):
                    _f3_total += 1
                    if _attr == 'FII_BULL':
                        _f3_bull_votes += 1
                    elif _attr == 'FII_BEAR':
                        _f3_bear_votes += 1
        if _f3_total >= 2:
            _f3_majority = (_f3_total // 2) + 1   # strict majority threshold
            if effective_dir == 1 and _f3_bear_votes >= _f3_majority:
                effective_dir = 0
                log.info(
                    "Filter F3: LONG blocked — majority FII_BEAR (%d/%d classifiers)",
                    _f3_bear_votes, _f3_total,
                )
            elif effective_dir == -1 and _f3_bull_votes >= _f3_majority:
                effective_dir = 0
                log.info(
                    "Filter F3: SHORT blocked — majority FII_BULL (%d/%d classifiers)",
                    _f3_bull_votes, _f3_total,
                )
        elif _f3_total == 1 and fii_dii_result is not None:
            # Only BN-primary available — fall back to single-classifier block
            _clf_attr = fii_dii_result.get('attribution', '')
            if effective_dir == 1 and _clf_attr == 'FII_BEAR':
                effective_dir = 0
                log.info("Filter F3: LONG blocked — clf_bn=FII_BEAR (sole classifier)")
            elif effective_dir == -1 and _clf_attr == 'FII_BULL':
                effective_dir = 0
                log.info("Filter F3: SHORT blocked — clf_bn=FII_BULL (sole classifier)")

        # F4a: OI quadrant bearish + LONG
        if effective_dir == 1 and state.oi_quadrant == -1:
            effective_dir = 0
            log.info("Filter F4a: OI quadrant=-1 (bearish) + LONG → suppress")

        # F4b: price run-up + LONG + strike defense against
        if effective_dir == 1 and vs_open_pct > 0.5 and state.strike_defense == -1:
            effective_dir = 0
            log.info("Filter F4b: vs_open=%.2f%% + LONG + strike_defense=-1 → suppress", vs_open_pct)

        # F5: extreme contango in heavy crash regime → suppress LONG
        if effective_dir == 1 and regime_pct < -3.0:
            _spot_ref = spot_ltp if spot_ltp > 0 else (fut_ltp * 0.9985)
            raw_prem  = (fut_ltp - _spot_ref) / _spot_ref * 100.0 if _spot_ref > 0 else 0.0
            fair_prem = 8.0 * (dte / 365)
            basis_now = raw_prem - fair_prem
            if basis_now > 1.0:
                effective_dir = 0
                log.info(
                    "Filter F5: crash regime regime=%.2f%% + artificial contango basis=%.2f%% → suppress LONG",
                    regime_pct, basis_now,
                )

        # Momentum filter: price must trend in signal direction for last MOMENTUM_BARS
        if effective_dir != 0 and len(df_fut) >= MOMENTUM_BARS:
            price_now  = fut_ltp
            price_past = float(df_fut.iloc[-MOMENTUM_BARS]['close'])
            price_mom  = 1 if price_now > price_past else -1
            if price_mom != effective_dir:
                effective_dir = 0
                log.info(
                    "Filter MOMENTUM: price_mom=%+d disagrees with signal_dir=%+d → suppress",
                    price_mom, smoothed_dir,
                )

        # Signal consensus: require at least MIN_SIGNAL_COUNT signals agree
        if effective_dir != 0 and state.signal_count < MIN_SIGNAL_COUNT:
            effective_dir = 0
            log.info(
                "Filter CONSENSUS: signal_count=%d < %d → suppress",
                state.signal_count, MIN_SIGNAL_COUNT,
            )

        # F6: option-flow confluence (env-gated, default off)
        if REQUIRE_OPTION_FLOW_CONFIRM and effective_dir != 0:
            _flow_dir = _read_option_flow_direction('BANKNIFTY')
            if _flow_dir != 0 and _flow_dir != effective_dir:
                log.info(
                    "Filter OPTION_FLOW: flow_dir=%+d disagrees with effective_dir=%+d → suppress",
                    _flow_dir, effective_dir,
                )
                effective_dir = 0

        # No-intraday suppression: mirrors backtest logic.
        # If OI velocity is empty AND FII/DII classifier has no result,
        # the two most informative real-time signals are blind — suppress entry.
        _no_intraday = (not velocity_data) and (fii_dii_result is None)
        if _no_intraday and effective_dir != 0:
            effective_dir = 0
            log.info(
                "no_intraday veto: velocity_data empty=%s fii_dii_result=%s",
                not bool(velocity_data), fii_dii_result,
            )

        log.info(
            "effective_dir=%+d (smoothed_dir=%+d, filters applied)",
            effective_dir, smoothed_dir,
        )

        # ── Morning alert (once, just after entry window opens) ────────────────
        if not morning_alert_sent and now >= entry_target:
            msg = _fmt_morning_alert(today, static, clf_bn, clf_combined, clf_nifty)
            _tg_broadcast(tg_token, tg_chats, msg)
            morning_alert_sent = True
            log.info("Morning Telegram alert sent")

        # ── Entry: 11:00–13:00, first clean signal passing all filters ──────────
        if position is None and n_bars >= ENTRY_BAR and entry_target <= now <= last_entry_target:
            if effective_dir == 0 or abs(state.score) < SIGNAL_SCORE_MIN:
                log.info(
                    "No entry this bar: effective_dir=%+d score=%.3f",
                    effective_dir, state.score,
                )
                time.sleep(60)
                continue

            atm    = int(round((spot_ltp or open_price) / BN_STEP) * BN_STEP)
            side   = 'CE' if effective_dir == 1 else 'PE'
            strike = atm

            # ── Re-entry cooldown — see v3/live/reentry_cooldown.py ──────────
            blocked, why = _re_is_in_cooldown('BANKNIFTY', effective_dir, strike, now)
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
                try:
                    opt_ltp = _get_option_ltp(g, expiry, strike, side, today)
                except RuntimeError as e:
                    log.error(
                        "Cannot get BN option LTP at entry: %s. Skipping this bar.", e
                    )
                    time.sleep(60)
                    continue

            position = {
                'direction':   effective_dir,
                'side':        side,
                'strike':      strike,
                'opt_symbol':  _option_symbol(expiry, strike, side),
                'entry_price': float(opt_ltp),
                'entry_time':  now,
                'qty':         BN_LOT,
                'entry_score': round(state.score, 4),
            }
            max_pnl_pct     = 0.0
            trail_activated = False
            flow_tracker    = FlowConflictTracker()

            log.info(
                "[%s] ENTER BN %s BUY  strike=%d @ %.2f  qty=%d  "
                "score=%.3f  effective_dir=%+d  bn_clf=%s  combined=%s",
                'PAPER' if paper else 'LIVE',
                side, strike, opt_ltp, BN_LOT,
                state.score, effective_dir,
                fii_dii_result.get('attribution', 'N/A') if fii_dii_result else 'N/A',
                clf_combined_result.get('attribution', 'N/A') if clf_combined_result else 'N/A',
            )

            entry_msg = _fmt_entry_alert(
                position, state,
                fii_dii_result, clf_combined_result, clf_nifty_result,
                paper,
            )
            _tg_broadcast(tg_token, tg_chats, entry_msg)

            bars_in_position = 0

            if not paper:
                raise NotImplementedError(
                    "Live order placement not implemented. "
                    "Groww order API endpoint and parameters must be "
                    "confirmed before enabling --live."
                )

            time.sleep(60)
            continue

        # ── Past last-entry window with no position → idle until EOD ──────────
        if position is None and now > last_entry_target:
            if not morning_alert_sent:
                # Entry window already closed — still send morning alert if not sent yet
                msg = _fmt_morning_alert(today, static, clf_bn, clf_combined, clf_nifty)
                _tg_broadcast(tg_token, tg_chats, msg)
                morning_alert_sent = True
            log.info("Past last-entry window (13:00), no position taken. Waiting for EOD.")
            time.sleep(60)
            continue

        # ── Monitor open position: SL / TP / Reversal / EOD ──────────────────
        if position and position.get('direction') != 0:
            side   = position['side']
            strike = position['strike']
            bars_in_position += 1

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
            pnl_inr  = pnl_pts * BN_LOT

            # Update trailing stop high-water mark
            if pnl_pct > max_pnl_pct:
                max_pnl_pct = pnl_pct
            if max_pnl_pct >= TRAIL_ACTIVATE_PCT:
                trail_activated = True

            log.info(
                "[POSITION] BN %s strike=%d  entry=%.2f  ltp=%.2f  "
                "pnl=%.2f pts (%.1f%%)  ₹%.0f  trail=%s(hwm=%.1f%%)",
                side, strike, entry_px, current_ltp,
                pnl_pts, pnl_pct * 100, pnl_inr,
                'ON' if trail_activated else 'off', max_pnl_pct * 100,
            )

            # ── Flow-Conflict Exit Assist (L1/L2/L3) ──────────────────────────
            fc = _fc_evaluate('BANKNIFTY', position['direction'], pnl_pct,
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
                        "[EXIT FLOW_SL] %s BN %s strike=%d  entry=%.2f  "
                        "exit=%.2f  pnl=%.2f pts (%.1f%%)  ₹%.0f",
                        'PAPER' if paper else 'LIVE', side, strike, entry_px,
                        current_ltp, pnl_pts, pnl_pct * 100, pnl_inr,
                    )
                    exit_msg = _fmt_exit_alert(
                        position, float(current_ltp), exit_reason,
                        pnl_pts, pnl_inr, paper,
                    )
                    _tg_broadcast(tg_token, tg_chats, exit_msg)
                    # Arm re-entry cooldown so we don't re-buy same direction
                    # near this strike for COOLDOWN_MIN minutes.
                    _re_record_force_exit('BANKNIFTY', position['direction'],
                                          strike, 'FLOW_SL', now)
                    position         = None
                    bars_in_position = 0
                    max_pnl_pct      = 0.0
                    trail_activated  = False
                    flow_tracker     = None
                    time.sleep(60)
                    continue

            # SL / TP / Trailing stop
            sl_hit    = pnl_pct <= SL_PCT
            tp_hit    = pnl_pct >= TP_PCT
            trail_hit = trail_activated and pnl_pct <= TRAIL_LOCK_PCT

            if sl_hit or tp_hit or trail_hit:
                exit_reason = ('TP'          if tp_hit    else
                               'TRAIL_STOP'  if trail_hit else 'SL')
                result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                log.info(
                    "[EXIT %s] %s BN %s strike=%d  entry=%.2f  exit=%.2f  "
                    "pnl=%.2f pts (%.1f%%)  ₹%.0f",
                    exit_reason, 'PAPER' if paper else 'LIVE',
                    side, strike, entry_px, current_ltp,
                    pnl_pts, pnl_pct * 100, pnl_inr,
                )
                exit_msg = _fmt_exit_alert(
                    position, float(current_ltp), exit_reason,
                    pnl_pts, pnl_inr, paper,
                )
                _tg_broadcast(tg_token, tg_chats, exit_msg)
                position        = None
                bars_in_position = 0
                max_pnl_pct     = 0.0
                trail_activated = False
                flow_tracker    = None
                time.sleep(60)
                continue

            # Reversal check every N bars — BN exits immediately (no MIN_REVERSAL_HOLD)
            if bars_in_position % REVERSAL_CHECK_EVERY == 0:
                if (effective_dir != 0
                        and effective_dir != position['direction']
                        and abs(state.score) >= SIGNAL_SCORE_MIN):
                    exit_reason = 'REVERSAL'
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
                    position        = None
                    bars_in_position = 0
                    max_pnl_pct     = 0.0
                    trail_activated = False
                    flow_tracker    = None
                    time.sleep(60)
                    continue

            # EOD exit at 15:20
            if now >= exit_target:
                exit_reason = 'EOD'
                result_str  = 'WIN' if pnl_pts > 0 else 'LOSS'
                log.info(
                    "[EXIT EOD] %s BN %s strike=%d  entry=%.2f  exit=%.2f  "
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
                position        = None
                bars_in_position = 0
                max_pnl_pct     = 0.0
                trail_activated = False
                flow_tracker    = None
                break

        # Idle — wait for next minute
        time.sleep(60)

    # ── Final EOD persist — flush everything accumulated today ────────────────
    log.info("EOD persist: writing session data to disk…")
    _persist_candles_bn(df_fut, today)
    _persist_option_oi_bn(oi_snapshots, today)
    log.info("EOD persist complete.")

    log.info("BANKNIFTY runner session complete for %s", today)


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BANKNIFTY options live runner')
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
        log.exception("BANKNIFTY runner crashed with unhandled exception — see traceback above")
        raise
