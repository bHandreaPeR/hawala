"""v3/live/flow_conflict.py — Flow-Conflict Exit Assist.

Watches the option-flow conviction (written every ~5s by
alerts/option_flow_daemon.py → v3/cache/option_flow_<INST>.json) against an
OPEN option position. When order-flow conviction accumulates AGAINST the
trade AND the trade is already losing past a threshold, it emits graduated
conflict alerts:

  L1 WATCH   — flow turned against you, mild drawdown.            (alert only)
  L2 REDUCE  — sustained conflict, real drawdown.                 (alert only)
  L3 EXIT    — conflict + deep drawdown / long-sustained conflict.
               This is the "premature stop": recommends — and, if
               FC_AUTOEXIT is on, forces — an early square-off well before
               the hard -50% premium SL.

Position direction convention (matches the runners):
  +1 = bullish bet (CE long)   -1 = bearish bet (PE long)
A "conflict" = option-flow conviction points opposite the position.

All thresholds are env-configurable so the behaviour can be tuned or
disabled without code changes. Set V3_FLOW_CONFLICT=0 to switch off entirely.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]

# ── Tunables ──────────────────────────────────────────────────────────────────
FC_ENABLED   = os.environ.get("V3_FLOW_CONFLICT", "1") == "1"
FC_AUTOEXIT  = os.environ.get("V3_FLOW_CONFLICT_SL", "1") == "1"  # L3 forces exit

# conviction (signed, position-frame) must be ≤ -FC_CONV_MIN to count as opposed
FC_CONV_MIN  = float(os.environ.get("V3_FC_CONV_MIN", "0.8"))

# drawdown gates (pnl_pct, negative = losing)
FC_L1_DD     = float(os.environ.get("V3_FC_L1_DD", "-0.06"))
FC_L2_DD     = float(os.environ.get("V3_FC_L2_DD", "-0.12"))
FC_L3_DD     = float(os.environ.get("V3_FC_L3_DD", "-0.18"))

# sustained-conflict gates (consecutive opposed runner cycles, ~60s each)
FC_L2_CONSEC = int(os.environ.get("V3_FC_L2_CONSEC", "3"))
FC_L3_CONSEC = int(os.environ.get("V3_FC_L3_CONSEC", "5"))

# option_flow_<INST>.json freshness
FC_MAX_AGE_SEC = int(os.environ.get("V3_FC_MAX_AGE_SEC", "90"))


class FlowConflictTracker:
    """Per-position state. Create one when a position opens, drop on exit."""

    def __init__(self) -> None:
        self.fired: set[int] = set()   # levels already alerted (latched)
        self.consec_opposed = 0        # consecutive opposed runner cycles

    def _bump(self, opposed: bool) -> None:
        self.consec_opposed = self.consec_opposed + 1 if opposed else 0


def _read_flow(inst: str) -> dict | None:
    """Return the fresh option-flow snapshot dict, or None if missing/stale."""
    try:
        p = ROOT / 'v3' / 'cache' / f'option_flow_{inst}.json'
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        ts = datetime.fromisoformat(d.get('ts', ''))
        age = (datetime.now(ts.tzinfo) - ts).total_seconds() if ts.tzinfo else 9999
        if age > FC_MAX_AGE_SEC:
            return None
        return d
    except Exception:
        return None


def evaluate(inst: str, position_direction: int, pnl_pct: float,
             tracker: FlowConflictTracker) -> dict | None:
    """Evaluate flow-conflict for one open position on one runner cycle.

    Args:
        inst                — 'NIFTY' | 'BANKNIFTY'
        position_direction  — +1 bullish (CE) / -1 bearish (PE)
        pnl_pct             — current trade P&L as a fraction (negative = loss)
        tracker             — FlowConflictTracker for this position

    Returns None if nothing to report, else a dict:
        {level, action, force_exit, conviction, band, consec, message}
    Only the HIGHEST not-yet-fired level fires per call; lower levels are
    latched so they never re-fire for the same position.
    """
    if not FC_ENABLED or position_direction == 0 or tracker is None:
        return None

    flow = _read_flow(inst)
    if flow is None:
        tracker._bump(False)
        return None

    conviction = float(flow.get('conviction', 0.0))
    band       = int(flow.get('conv_band', 0))

    # project flow into the position's frame: negative = against the trade
    aligned_conv = position_direction * conviction
    aligned_band = position_direction * band
    opposed = aligned_conv <= -FC_CONV_MIN

    tracker._bump(opposed)
    if not opposed:
        return None

    consec      = tracker.consec_opposed
    strong_band = aligned_band <= -2          # STRONG/EXTREME band against us

    # ── decide highest applicable level ──────────────────────────────────────
    level = 0
    if pnl_pct <= FC_L1_DD:
        level = 1
    if pnl_pct <= FC_L2_DD and (consec >= FC_L2_CONSEC or strong_band):
        level = 2
    if (pnl_pct <= FC_L3_DD) or (consec >= FC_L3_CONSEC and pnl_pct <= FC_L2_DD) \
            or (strong_band and pnl_pct <= FC_L2_DD):
        level = 3

    if level == 0 or level in tracker.fired:
        # nothing new — but if a higher level already fired, suppress lower noise
        return None

    # latch this and all lower levels
    for lv in range(1, level + 1):
        tracker.fired.add(lv)

    action     = {1: 'WATCH', 2: 'REDUCE', 3: 'EXIT'}[level]
    force_exit = level == 3 and FC_AUTOEXIT

    return {
        'level': level, 'action': action, 'force_exit': force_exit,
        'conviction': conviction, 'band': band, 'consec': consec,
        'message': _format(inst, level, action, position_direction,
                           pnl_pct, conviction, band, consec, force_exit),
    }


def _format(inst: str, level: int, action: str, pos_dir: int,
            pnl_pct: float, conviction: float, band: int,
            consec: int, force_exit: bool) -> str:
    """Build the Telegram alert body for a conflict level."""
    icon = {1: '🟡', 2: '🟠', 3: '🔴'}[level]
    bet  = 'BULLISH (CE)' if pos_dir > 0 else 'BEARISH (PE)'
    flow_dir = 'bullish' if conviction > 0 else 'bearish'
    band_txt = {0: 'building', 1: 'STRONG', 2: 'EXTREME'}.get(abs(band), 'building')

    head = {
        1: f"{icon} <b>FLOW CONFLICT L1 — WATCH</b>",
        2: f"{icon} <b>FLOW CONFLICT L2 — REDUCE</b>",
        3: f"{icon} <b>FLOW CONFLICT L3 — EXIT</b>",
    }[level]

    lines = [
        head,
        f"{inst} — your bet: {bet}",
        f"Option flow conviction: {conviction:+.2f} ({flow_dir}, {band_txt}) "
        f"— opposing your trade for {consec} cycle(s)",
        f"Position P&L: {pnl_pct * 100:+.1f}%",
    ]
    if level == 1:
        lines.append("Flow has turned against the position. Watch closely.")
    elif level == 2:
        lines.append("Conflict is sustained and the trade is losing. "
                      "Consider cutting size or tightening the stop.")
    else:
        if force_exit:
            lines.append("⚠️ Premature stop TRIGGERED — squaring off now, "
                          "ahead of the hard -50% SL.")
        else:
            lines.append("⚠️ Strong opposing flow + deep drawdown. "
                          "Recommend immediate manual square-off.")
    return "\n".join(lines)
