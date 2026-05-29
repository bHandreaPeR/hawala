"""Rolling macro SENTIMENT — a stable, recency-weighted mood gauge for the
viewer's MACRO positioning card.

Distinct from the live PULSE (news_signal.json score/confidence), which uses a
3-minute half-life so a fresh headline spikes the ALERT path. A positioning
gauge should instead integrate the last 24h of scored news with a long
half-life (recency bias via exponential decay = the continuous form of an EMA),
so a big recent event still moves it while a day of tone persists.

We can't read 24h from event_clusters (pruned at CLUSTER_TTL_MIN=90m), so we
keep our own tiny append-only log of ONLY scored items (a few hundred/day).

    sentiment_score      = Σ(score·w) / Σ(|score|·w)        ∈ [-1, +1]  (net tilt)
    sentiment_confidence = min(1, Σ(|score|·w) / SAT)       (intensity, saturating)
    w = exp(-age_min / (half_life_h·60))

The card value = sentiment_score × sentiment_confidence (same shape the viewer
already expects), so one strong fresh bearish event pulls it negative and it
eases back toward 0 over hours unless corroborated.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from .dedup import IST

STATE_DIR = Path(__file__).resolve().parent / "state"
LOG_PATH  = STATE_DIR / "sentiment_log.ndjson"

WINDOW_H    = float(os.environ.get("NEWS_SENTIMENT_WINDOW_H",   "24"))
HALF_LIFE_H = float(os.environ.get("NEWS_SENTIMENT_HALFLIFE_H", "6"))
SAT         = float(os.environ.get("NEWS_SENTIMENT_SAT",        "2.0"))
PRUNE_H     = 48.0   # keep a bit more than the window so prune isn't lossy


def _now() -> datetime:
    return datetime.now(IST)


def _parse_ts(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt
    except Exception:
        return None


def record(s: dict) -> None:
    """Append one SCORED item (event_class set, score != 0). No-op otherwise.
    `s` is a scorer.score_headline() dict."""
    if not s or not s.get("event_class") or float(s.get("score", 0.0)) == 0.0:
        return
    rec = {
        "ts":    (s.get("ts_seen") or _now().isoformat()),
        "dir":   int(s.get("direction", 0)),
        "score": float(s.get("score", 0.0)),   # ≈ undecayed base (decay≈1 at scoring)
        "tier":  float(s.get("tier", 0.7)),
        "cls":   s.get("event_class"),
    }
    if isinstance(rec["ts"], datetime):
        rec["ts"] = rec["ts"].isoformat()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _read(now: datetime) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    keep_cut = now - timedelta(hours=PRUNE_H)
    out, pruned_any = [], False
    try:
        for ln in LOG_PATH.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            dt = _parse_ts(r.get("ts"))
            if dt is None:
                continue
            if dt < keep_cut:
                pruned_any = True
                continue
            r["_dt"] = dt
            out.append(r)
    except Exception:
        return []
    # Opportunistic prune: rewrite without the aged-out lines.
    if pruned_any:
        try:
            LOG_PATH.write_text(
                "".join(json.dumps({k: v for k, v in r.items() if k != "_dt"},
                                   separators=(",", ":")) + "\n" for r in out))
        except Exception:
            pass
    return out


def rolling(now: datetime | None = None) -> dict:
    """Compute the rolling sentiment over the last WINDOW_H hours."""
    now = now or _now()
    rows = _read(now)
    cutoff = now - timedelta(hours=WINDOW_H)
    hl_min = max(1.0, HALF_LIFE_H * 60.0)
    num = den = intensity = 0.0
    n = 0
    for r in rows:
        dt = r.get("_dt")
        if dt is None or dt < cutoff:
            continue
        sc = float(r.get("score", 0.0))
        if sc == 0.0:
            continue
        age_min = max(0.0, (now - dt).total_seconds() / 60.0)
        w = math.exp(-age_min / hl_min)
        num += sc * w
        den += abs(sc) * w
        intensity += abs(sc) * w
        n += 1
    score = (num / den) if den > 0 else 0.0
    conf  = min(1.0, intensity / SAT) if SAT > 0 else 0.0
    return {
        "sentiment_score":      round(score, 4),
        "sentiment_confidence": round(conf, 4),
        "sentiment_n":          n,
        "sentiment_window_h":   WINDOW_H,
        "sentiment_half_life_h": HALF_LIFE_H,
    }
