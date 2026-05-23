"""v3/live/reentry_cooldown.py — Block re-entries shortly after a force-exit.

Background
----------
On Fri 22-May-2026 the BANKNIFTY v3 runner entered LONG CE 54100 at 11:46,
got force-exited at 12:24 by the flow-conflict daemon (option-flow had
flipped bearish, lost ₹1,677), then RE-ENTERED LONG CE 54100 at 12:50 —
26 minutes later. That second entry got force-exited again at 14:38 for
−27.5% (₹3,922 more lost). Total ₹5,599 lost from buying calls into a
falling market twice within an hour.

The v3 signal engine is stateless about recent force-exits — its score
said LONG both times because the daily FII_BULL classifier hadn't flipped
yet. The flow-conflict daemon only fires on OPEN positions, never pre-vetoes.

This module fills the gap. After a FLOW_SL force-exit:
  - record (instrument, direction, strike, ts) to a state file
  - subsequent entries within COOLDOWN_MIN minutes on same direction
    AND strike within ±STRIKE_TOLERANCE_PTS are blocked

Logic is conservative: only blocks same-direction same-strike-area entries.
Opposite-direction or far-strike entries pass through.

Env overrides (defaults are reasonable):
  V3_COOLDOWN_MIN              90       # minutes after force-exit
  V3_COOLDOWN_STRIKE_TOL_NIFTY 200      # ±2 strikes (50-pt grid)
  V3_COOLDOWN_STRIKE_TOL_BN    400      # ±2 strikes (100-pt grid)
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timedelta
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / 'v3' / 'cache' / 'reentry_cooldown.json'

COOLDOWN_MIN          = int(os.environ.get('V3_COOLDOWN_MIN', '90'))
STRIKE_TOLERANCE_PTS  = {
    'NIFTY':     int(os.environ.get('V3_COOLDOWN_STRIKE_TOL_NIFTY', '200')),
    'BANKNIFTY': int(os.environ.get('V3_COOLDOWN_STRIKE_TOL_BN',    '400')),
    'SENSEX':    int(os.environ.get('V3_COOLDOWN_STRIKE_TOL_SX',    '400')),
}


# ─── Persistence ─────────────────────────────────────────────────────────────
def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


# ─── Public API ──────────────────────────────────────────────────────────────
def record_force_exit(inst: str, direction: int, strike: int,
                       exit_reason: str = 'FLOW_SL',
                       ts: Optional[datetime] = None) -> None:
    """Call this AFTER a force-exit. Stores the (direction, strike, ts) so
    future entry checks can block re-entries during the cooldown window."""
    state = _load()
    state[inst] = {
        'last_force_exit_ts': (ts or datetime.now()).isoformat(),
        'direction':  int(direction),
        'strike':     int(strike),
        'exit_reason': exit_reason,
    }
    _save(state)


def is_in_cooldown(inst: str, direction: int, strike: int,
                    ts: Optional[datetime] = None) -> tuple[bool, str]:
    """Returns (blocked, reason_string).

    Blocked if ALL true:
      - last force-exit on `inst` was within COOLDOWN_MIN minutes
      - same direction (+1 vs +1, -1 vs -1)
      - strike within ±STRIKE_TOLERANCE_PTS of last exited strike

    Otherwise returns (False, '').
    """
    state = _load()
    rec = state.get(inst)
    if not rec:
        return False, ''

    try:
        last_ts = datetime.fromisoformat(rec['last_force_exit_ts'])
    except Exception:
        return False, ''

    now = ts or datetime.now()
    if last_ts.tzinfo is not None and now.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=None)
    elapsed_min = (now - last_ts).total_seconds() / 60.0
    if elapsed_min >= COOLDOWN_MIN:
        return False, ''

    if int(rec.get('direction', 0)) != int(direction):
        return False, ''

    tol = STRIKE_TOLERANCE_PTS.get(inst, 400)
    if abs(int(rec.get('strike', 0)) - int(strike)) > tol:
        return False, ''

    return True, (
        f"reentry cooldown: {inst} dir={direction} strike={strike} blocked "
        f"({elapsed_min:.0f}min since {rec.get('exit_reason','?')} on "
        f"strike={rec['strike']}; cooldown={COOLDOWN_MIN}min)"
    )


def clear(inst: Optional[str] = None) -> None:
    """Reset cooldown state. Use only in testing or manual override."""
    if inst is None:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return
    state = _load()
    state.pop(inst, None)
    _save(state)
