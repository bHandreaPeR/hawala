"""
v3/data/oi_cache_merge.py
=========================
Shared merge logic for the per-day option-OI cache so historical-candle
fetchers never destroy real OI.

Background: Groww's get_historical_candles omits the OI column for a still-
ACTIVE (unexpired) weekly/monthly contract — it returns 6 cols, not 7. So any
fetch run on the session day (or any day before the contract expires) yields
oi=0. The live writers (option_flow_daemon, the runners) source real OI from
get_option_chain (open_interest) every minute. If a same-day historical fetch
then writes its day entry wholesale, it would overwrite that real OI with zero.

merge_day_oi() resolves the two sources per strike-and-side with this rule:
  1. incoming has real OI (any oi>0)   → use incoming  (complete: expired
                                          backfill is the gold standard)
  2. else existing has real OI         → keep existing  (preserve live OI)
  3. else (neither has OI)             → use incoming  (fresher close/volume)

This is idempotent and order-independent for the (real vs zero) cases that
matter, so it's safe to call on every write.
"""
from __future__ import annotations

import pandas as pd

_COLS = ['ts', 'close', 'volume', 'oi', 'oi_raw']


def _has_real_oi(df) -> bool:
    """True if df is a non-empty DataFrame with at least one oi > 0."""
    if df is None or not hasattr(df, 'columns') or 'oi' not in df.columns:
        return False
    if len(df) == 0:
        return False
    s = pd.to_numeric(df['oi'], errors='coerce')
    return bool((s > 0).any())


def _pick_side(existing, incoming):
    """Choose the better DataFrame for one strike-side per the module rule."""
    if _has_real_oi(incoming):
        return incoming
    if _has_real_oi(existing):
        return existing
    # Neither has OI — prefer incoming if it carries data, else existing.
    if incoming is not None and hasattr(incoming, 'columns') and len(incoming):
        return incoming
    if existing is not None and hasattr(existing, 'columns') and len(existing):
        return existing
    return incoming if incoming is not None else existing


def merge_day_oi(existing_day: dict | None, incoming_day: dict | None) -> dict:
    """Merge one day's option-OI entries, preserving real OI over zero-OI.

    Each arg is {strike: {'CE': DataFrame, 'PE': DataFrame}} (or None/empty).
    Returns a new dict; inputs are not mutated.
    """
    existing_day = existing_day or {}
    incoming_day = incoming_day or {}
    if not existing_day:
        return dict(incoming_day)
    if not incoming_day:
        return dict(existing_day)

    merged: dict = {}
    for strike in set(existing_day) | set(incoming_day):
        e_sides = existing_day.get(strike) or {}
        i_sides = incoming_day.get(strike) or {}
        out: dict = {}
        for side in ('CE', 'PE'):
            out[side] = _pick_side(e_sides.get(side), i_sides.get(side))
        merged[strike] = out
    return merged
