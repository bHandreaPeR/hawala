"""
v3/signals/option_flow.py — Strike-level option OI flow classifier.

The retail-Indian-market analog to Bookmap's Cumulative Volume Delta.

For each strike in the option chain, two snapshots N seconds apart give us:
    Δp  = current_LTP   − previous_LTP
    ΔOI = current_OI    − previous_OI

The sign-combination of these two yields one of FOUR positioning states
per side (CE / PE). The semantic interpretation is:

    CALLS (CE)
        Δp>0, ΔOI>0  →  long_call_build   :  BULLISH  (call buyers active)
        Δp>0, ΔOI<0  →  short_call_cover  :  BULLISH  (call writers buying back)
        Δp<0, ΔOI>0  →  short_call_build  :  BEARISH  (call writers selling premium)
        Δp<0, ΔOI<0  →  long_call_unwind  :  BEARISH  (call buyers exiting)

    PUTS  (PE)
        Δp>0, ΔOI>0  →  long_put_build    :  BEARISH  (put buyers active)
        Δp>0, ΔOI<0  →  short_put_cover   :  BEARISH  (put writers buying back)
        Δp<0, ΔOI>0  →  short_put_build   :  BULLISH  (put writers selling premium)
        Δp<0, ΔOI<0  →  long_put_unwind   :  BULLISH  (put buyers exiting)

(Direction is relative to the *underlying*, not the option price.)

Each strike contributes a directional score weighted by:
    cash_change   = |ΔOI| × LTP            — rupee-value of position change
    atm_proximity = max(0, 1 - 10·|K-S|/S)  — strikes >10% from spot ignored
    weight        = cash_change × atm_proximity

Net flow = Σ(sign × weight) across all strikes in chain.
This is the per-snapshot delta. Session CVD = cumulative sum since 09:15.

PUBLIC SURFACE
    classify_ce(dp, doi) -> str               # 'long_call_build' …
    classify_pe(dp, doi) -> str
    score_snapshot(prev, curr, lot_size=1) -> dict
    cumulative_state_update(state, snapshot_score) -> state
    extreme_signal(score, history) -> str | None  # 'z_high'|'flip'|'sustained'|None
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

# ── State → directional sign for the UNDERLYING ──────────────────────────────
FLOW_DIRECTION: dict[str, int] = {
    # CE
    'long_call_build':  +1,
    'short_call_cover': +1,
    'short_call_build': -1,
    'long_call_unwind': -1,
    # PE
    'long_put_build':   -1,
    'short_put_cover':  -1,
    'short_put_build':  +1,
    'long_put_unwind':  +1,
}

# Only strikes within this fraction of spot contribute (10%)
_ATM_BAND_FRAC = 0.10

# ε to break tie on dp/doi ≈ 0 — must be small enough not to hide real changes
_EPS_PRICE = 0.05    # rupees per option (LTP rounded to 0.05 on Indian exchanges)
_EPS_OI    = 1.0     # one OI unit


# ── Classification primitives ────────────────────────────────────────────────
def classify_ce(dp: float, doi: float) -> Optional[str]:
    """Classify a CE strike's flow state. Returns None on no change."""
    if abs(dp) < _EPS_PRICE and abs(doi) < _EPS_OI:
        return None
    if doi > 0:
        return 'long_call_build' if dp > 0 else 'short_call_build'
    if doi < 0:
        return 'short_call_cover' if dp > 0 else 'long_call_unwind'
    return None   # OI flat → no positioning signal even if premium ticked


def classify_pe(dp: float, doi: float) -> Optional[str]:
    """Classify a PE strike's flow state. Returns None on no change."""
    if abs(dp) < _EPS_PRICE and abs(doi) < _EPS_OI:
        return None
    if doi > 0:
        return 'long_put_build' if dp > 0 else 'short_put_build'
    if doi < 0:
        return 'short_put_cover' if dp > 0 else 'long_put_unwind'
    return None


def atm_proximity(strike: float, spot: float) -> float:
    """0..1 weight; 1 at spot, 0 beyond ±10% of spot."""
    if spot <= 0:
        return 0.0
    rel = abs(strike - spot) / spot
    return max(0.0, 1.0 - rel / _ATM_BAND_FRAC)


# ── Per-snapshot scoring ─────────────────────────────────────────────────────
def score_snapshot(prev: dict, curr: dict, lot_size: int = 1) -> dict:
    """Compute the directional flow delta between two option-chain snapshots.

    Inputs are the dicts returned by runner._fetch_option_chain:
        {
          'underlying_ltp': float,
          'strikes':        list[int|float],
          'ce_oi':          {strike: float},
          'pe_oi':          {strike: float},
          'ce_ltp':         {strike: float},
          'pe_ltp':         {strike: float},
          ...
        }

    Returns:
        {
          'spot':          float,
          'net':           float,   # signed flow (bullish > 0)
          'bull':          float,   # sum of bullish contributions
          'bear':          float,   # sum of bearish contributions
          'top_strikes':   list[dict],  # 5 largest contributors
          'state_counts':  dict[state → count],
        }
    """
    spot = float(curr.get('underlying_ltp', 0) or 0)
    strikes = curr.get('strikes', []) or []

    bull = bear = 0.0
    contributions: list[tuple[float, dict]] = []
    state_counts: dict[str, int] = {}

    for K in strikes:
        prox = atm_proximity(float(K), spot)
        if prox <= 0:
            continue   # OTM beyond band

        for side in ('ce', 'pe'):
            p_now  = float(curr.get(f'{side}_ltp', {}).get(K, 0) or 0)
            p_prev = float(prev.get(f'{side}_ltp', {}).get(K, 0) or 0)
            oi_now  = float(curr.get(f'{side}_oi', {}).get(K, 0) or 0)
            oi_prev = float(prev.get(f'{side}_oi', {}).get(K, 0) or 0)

            dp  = p_now - p_prev
            doi = oi_now - oi_prev

            cls = classify_ce(dp, doi) if side == 'ce' else classify_pe(dp, doi)
            if cls is None:
                continue

            # Rupee value of the position change
            cash = abs(doi) * lot_size * max(p_now, 0.05)
            weight = cash * prox
            sign = FLOW_DIRECTION[cls]
            contrib = sign * weight

            if sign > 0: bull += weight
            else:        bear += weight

            state_counts[cls] = state_counts.get(cls, 0) + 1
            contributions.append((abs(contrib), {
                'strike':  float(K),
                'side':    side.upper(),
                'state':   cls,
                'sign':    sign,
                'weight':  weight,
                'dp':      dp,
                'doi':     doi,
            }))

    # Top 5 by absolute contribution
    contributions.sort(key=lambda x: x[0], reverse=True)
    top = [c[1] for c in contributions[:5]]

    return {
        'spot':         spot,
        'net':          bull - bear,
        'bull':         bull,
        'bear':         bear,
        'top_strikes':  top,
        'state_counts': state_counts,
    }


# ── Cumulative session state ─────────────────────────────────────────────────
def empty_state() -> dict:
    """Initial daily state for an instrument."""
    return {
        'cvd':         0.0,       # cumulative net flow since session open
        'last_cvd':    0.0,       # for sign-flip detection
        'history':     [],        # rolling list of recent 'net' values
        'sustained':   0,         # consecutive same-direction snapshots
        'sustained_dir': 0,       # +1/-1
        'last_alert_ts': None,    # for cooldown
        'last_alert_kind': None,
        # ── Conviction accumulator (May-2026 redesign) ──────────────────
        'conviction':        0.0, # EMA-weighted signed conviction
        'conv_peak':         0.0, # max |conviction| reached today
        'conv_band':         0,   # current band: -3..+3
        'conv_alerted_band': 0,   # last band an alert fired for
        'anomaly_count':     0,   # total anomalies that fed conviction
        'consec_anom_dir':   0,   # current anomaly streak direction
        'consec_anom_n':     0,   # length of that streak
    }


def cumulative_state_update(state: dict, snapshot: dict,
                            history_window: int = 240) -> dict:
    # window=240 snapshots ≈ 20 min at 5-sec polling. Calibrated May-18:
    # 60-snapshot (5-min) window flagged 8% of snapshots as z-outliers —
    # option flow has fat tails, short window = constant false bursts.
    """Fold a new snapshot score into the running daily state."""
    net = float(snapshot.get('net', 0.0))
    state['last_cvd'] = float(state.get('cvd', 0.0))
    state['cvd'] = state['last_cvd'] + net

    h = state.get('history', [])
    h.append(net)
    if len(h) > history_window:
        h = h[-history_window:]
    state['history'] = h

    # Sustained-direction tracker
    cur_dir = 0 if abs(net) < 1.0 else (1 if net > 0 else -1)
    if cur_dir != 0 and cur_dir == state.get('sustained_dir', 0):
        state['sustained'] = int(state.get('sustained', 0)) + 1
    else:
        state['sustained'] = 1 if cur_dir != 0 else 0
        state['sustained_dir'] = cur_dir

    return state


# ════════════════════════════════════════════════════════════════════════════
#  CONVICTION ACCUMULATOR  (May-2026 redesign)
# ════════════════════════════════════════════════════════════════════════════
# Philosophy: poll every 5s, but DON'T alert every 5s. Each tick is tested
# for a TRUE anomaly (outlier flow vs rolling baseline). Anomalies — and only
# anomalies — feed a signed EMA "conviction" that captures the *cumulative
# directional buildup*. Recent anomalies weigh more (EMA alpha). Quiet ticks
# decay conviction toward zero. An alert fires only when conviction crosses
# into a HIGHER band or flips direction — i.e. on a buildup STATE change,
# typically 2-5 times per session. Quality over quantity.
#
#   tick → score_snapshot → detect_anomaly → update_conviction → conviction_signal
#
# Tunables (daemon exposes as env vars):
#   CONV_ALPHA      weight on newest anomaly         (default 0.25)
#   CONV_DECAY      per-quiet-tick conviction decay   (default 0.02)
#   CONV_Z_MIN      z-score bar to call a tick anomalous (default 2.5)

# Conviction bands — |conviction| is roughly an EMA of capped z-scores (0..6).
_CONV_BANDS = [
    (3.5, 3, 'EXTREME'),
    (2.0, 2, 'STRONG'),
    (1.0, 1, 'BUILDING'),
    (0.0, 0, 'neutral'),
]


def _conv_band(conviction: float) -> int:
    """Map signed conviction → signed band level -3..+3."""
    a = abs(conviction)
    for thr, lvl, _ in _CONV_BANDS:
        if a >= thr:
            return lvl * (1 if conviction > 0 else -1) if lvl else 0
    return 0


def band_name(band: int) -> str:
    for _, lvl, name in _CONV_BANDS:
        if lvl == abs(band):
            return name
    return 'neutral'


def detect_anomaly(snapshot: dict, state: dict,
                   z_min: float = 2.5) -> Optional[dict]:
    """Is this 5-sec snapshot a TRUE flow outlier?

    Tests the snapshot's net flow against the rolling history baseline.
    A genuine anomaly = net flow this tick is ≥ z_min sigma from the
    20-min baseline. Returns {dir, magnitude, z} or None (normal tick).

    `magnitude` is the |z| capped at 6 — feeds the conviction EMA so a
    bigger outlier builds conviction faster.
    """
    net  = float(snapshot.get('net', 0.0))
    hist = state.get('history', [])
    if len(hist) < 30:           # need a baseline first
        return None
    base = hist[:-1]
    mu   = statistics.fmean(base)
    sd   = statistics.pstdev(base) or 1.0
    if sd <= 0:
        return None
    z = (net - mu) / sd
    if abs(z) < z_min:
        return None              # normal tick — contributes nothing
    return {
        'dir':       1 if net > 0 else -1,
        'magnitude': min(abs(z), 6.0),
        'z':         z,
    }


def update_conviction(state: dict, snapshot: dict,
                      alpha: float = 0.25, decay: float = 0.02,
                      z_min: float = 2.5) -> dict:
    """Fold this tick into the EMA conviction accumulator.

    - Anomalous tick → conviction = alpha·(signed magnitude) + (1-alpha)·prev
      (recent anomalies dominate; same-direction anomalies reinforce, opposite
      ones pull conviction back through zero).
    - Quiet tick → conviction *= (1 - decay)   (stale buildup fades).

    Mutates and returns `state`.
    """
    anom = detect_anomaly(snapshot, state, z_min)
    if anom is None:
        state['conviction'] = float(state.get('conviction', 0.0)) * (1.0 - decay)
    else:
        signed = anom['dir'] * anom['magnitude']
        prev   = float(state.get('conviction', 0.0))
        state['conviction'] = alpha * signed + (1.0 - alpha) * prev
        state['anomaly_count'] = int(state.get('anomaly_count', 0)) + 1
        if anom['dir'] == state.get('consec_anom_dir', 0):
            state['consec_anom_n'] = int(state.get('consec_anom_n', 0)) + 1
        else:
            state['consec_anom_dir'] = anom['dir']
            state['consec_anom_n']   = 1
        state['_last_anomaly'] = anom

    conv = float(state['conviction'])
    if abs(conv) > abs(float(state.get('conv_peak', 0.0))):
        state['conv_peak'] = conv
    state['conv_band'] = _conv_band(conv)
    return state


def conviction_signal(state: dict) -> Optional[str]:
    """Decide whether the conviction buildup warrants an alert.

    Only STRONG (band ±2) and EXTREME (±3) are alert-worthy — BUILDING
    (±1) is a watch state, not an alert. Two firing conditions:
      - escalation : conviction reached a STRONG+ band, higher than the
                     band we last alerted at
      - flip       : conviction is STRONG+ on the opposite side of the
                     last alert

    HYSTERESIS: once an alert fires at band B, `conv_alerted_band` stays
    at B until conviction fully decays back to NEUTRAL (band 0). Only then
    is it reset, so a fresh climb can alert again. This kills the
    0↔±1↔±2 oscillation that otherwise re-fires the same buildup all day.

    Returns 'conviction_escalate' / 'conviction_flip' / None.
    Expected ~3-8 alerts per instrument per session.
    """
    band    = int(state.get('conv_band', 0))
    alerted = int(state.get('conv_alerted_band', 0))

    # Conviction fully decayed to neutral → reset the alerted memory.
    if band == 0:
        state['conv_alerted_band'] = 0
        return None

    # BUILDING (±1) — watch only, never alert. Don't touch alerted memory.
    if abs(band) < 2:
        return None

    # Direction flip — STRONG+ on the opposite side of a prior alert.
    if alerted != 0 and (band * alerted < 0):
        state['conv_alerted_band'] = band
        return 'conviction_flip'

    # Escalation — STRONG+ band, stronger than last alerted band.
    if abs(band) > abs(alerted):
        state['conv_alerted_band'] = band
        return 'conviction_escalate'

    # Same or weaker band, already alerted — silent (hysteresis holds).
    return None


# ── Extreme-signal detection (LEGACY — superseded by conviction system) ──────
# Calibrated against Friday May 15 live data:
#   NIFTY end-of-session CVD = 7.3e8, max per-cycle net = 2.4e8
#   BANKNIFTY end-of-session CVD = 2.1e8, typical per-cycle net = 1e7
# Old defaults (2000 / 500) were 10^5× too small → never fired.
# New defaults set at ~0.1-0.5% of typical session scale.
#
# For adaptive scale-free firing, prefer z_threshold path which compares to
# rolling history rather than absolute number.
def extreme_signal(snapshot: dict, state: dict,
                   z_threshold: float = 3.5,
                   flip_threshold: float = 1_000_000.0,
                   sustain_min_bars: int = 5,
                   sustain_min_net: float = 500_000.0) -> Optional[str]:
    """
    Returns one of:
        'flip'      — CVD just crossed zero with magnitude > flip_threshold
        'z_high'    — |z-score of this snapshot's net| > z_threshold
        'sustained' — sustain_min_bars consecutive same-direction snapshots,
                      each with |net| > sustain_min_net
        None        — nothing notable
    """
    cvd      = float(state.get('cvd', 0.0))
    last_cvd = float(state.get('last_cvd', 0.0))
    net      = float(snapshot.get('net', 0.0))

    # 1. Sign flip with conviction
    if last_cvd * cvd < 0 and abs(cvd) >= flip_threshold:
        return 'flip'

    # 2. z-score extreme (compare to rolling history)
    hist = state.get('history', [])
    if len(hist) >= 10:
        mu  = statistics.fmean(hist[:-1])     # exclude current
        sd  = statistics.pstdev(hist[:-1]) or 1.0
        if sd > 0:
            z = (net - mu) / sd
            if abs(z) >= z_threshold:
                return 'z_high'

    # 3. Sustained one-sided pressure
    if (state.get('sustained', 0) >= sustain_min_bars
        and abs(net) >= sustain_min_net):
        return 'sustained'

    return None


def z_score(snapshot: dict, state: dict) -> float:
    """Return current z-score (for telemetry / message formatting)."""
    net = float(snapshot.get('net', 0.0))
    hist = state.get('history', [])
    if len(hist) < 5:
        return 0.0
    try:
        mu  = statistics.fmean(hist[:-1])
        sd  = statistics.pstdev(hist[:-1]) or 1.0
        return (net - mu) / sd if sd > 0 else 0.0
    except Exception:
        return 0.0
