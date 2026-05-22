"""Telegram alert + JSON state writer.

`update_signal_file()` writes the current global aggregate to
v3/cache/news_signal.json so v3 (or humans) can read the latest news state.

`maybe_alert()` decides whether to fire a Telegram alert and, if so, builds
the human-readable message and posts it.

Alert thresholds (tunable via env):
  NEWS_ALERT_SCORE_MIN   default 0.4
  NEWS_ALERT_CONF_MIN    default 0.6
"""
from __future__ import annotations

import argparse
import html as _html_lib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import dedup
from . import position_context as PC
from .dedup import IST
from .aggregator import aggregate_global, aggregate_cluster

log = logging.getLogger("news.dispatcher")

ROOT = Path(__file__).resolve().parent.parent
SIGNAL_PATH = ROOT / "v3" / "cache" / "news_signal.json"

ALERT_SCORE_MIN = float(os.environ.get("NEWS_ALERT_SCORE_MIN", "0.60"))
ALERT_CONF_MIN  = float(os.environ.get("NEWS_ALERT_CONF_MIN",  "0.70"))

# Tier-1 fast-path: when the top headline is from a Tier-1 source (Reuters,
# Bloomberg, FT, WSJ, Axios, Moneycontrol breaking, RBI), waiting for
# corroboration from Tier-2/3 is too slow. A Tier-1 single source carrying a
# strong score IS the alert. These thresholds gate this fast path.
TIER1_FASTPATH_SCORE_MIN = float(os.environ.get("NEWS_T1_SCORE_MIN", "0.70"))
TIER1_MIN_TIER           = float(os.environ.get("NEWS_T1_MIN_TIER",  "1.0"))

# Rate caps (May 2026 cleanup) — when a cap fires, the item is buffered
# for the EOD digest, not silently dropped.
NEWS_MAX_PER_HOUR       = int(os.environ.get("NEWS_MAX_PER_HOUR",       "4"))
NEWS_MAX_PER_DAY        = int(os.environ.get("NEWS_MAX_PER_DAY",       "12"))
NEWS_THEME_COOLDOWN_MIN = int(os.environ.get("NEWS_THEME_COOLDOWN_MIN","120"))

# Theme cooldown state (separate from event_key dedup)
THEME_ALERTED_PATH = ROOT / "news" / "state" / "theme_alerted.json"


# ── Telegram config ──────────────────────────────────────────────────────────
def _load_telegram_config() -> tuple[str, list[str]]:
    """News alerts → MACRO channel (context, not trade signals).
    Falls back to TRADE bot if MACRO tokens missing."""
    env_path = ROOT / "token.env"
    if not env_path.exists():
        return "", []
    env: dict = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    # Prefer MACRO bot for news (architectural rule: TRADE = entries/exits only)
    token    = (env.get("TELEGRAM_BOT_TOKEN_MACRO", "").strip()
                or env.get("TELEGRAM_BOT_TOKEN", "").strip())
    chat_raw = (env.get("TELEGRAM_CHAT_IDS_MACRO", "").strip()
                or env.get("TELEGRAM_CHAT_IDS", env.get("TELEGRAM_CHAT_ID", "")).strip())
    chats = [c.strip() for c in chat_raw.split(",") if c.strip()]
    return token, chats


def _tg_send(token: str, chat_id: str, text: str) -> bool:
    """Reuse alerts.telegram if importable; fall back to inline requests."""
    try:
        from alerts.telegram import send  # type: ignore
        return bool(send(token, chat_id, text))
    except Exception:
        import requests
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            return r.ok
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False


def _broadcast(token: str, chats: list[str], text: str) -> int:
    if not token or not chats:
        return 0
    n = 0
    for c in chats:
        if _tg_send(token, c, text):
            n += 1
    return n


# ── Signal-file writer ───────────────────────────────────────────────────────
def update_signal_file(global_agg: dict) -> None:
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(IST)
    payload = {
        "score":         global_agg.get("score", 0.0),
        "confidence":    global_agg.get("confidence", 0.0),
        "n_clusters":    global_agg.get("n_clusters", 0),
        "top_cluster":   _slim_cluster(global_agg.get("top_cluster")),
        "all_clusters":  [_slim_cluster(c) for c in global_agg.get("clusters", [])],
        "updated_at":    now.isoformat(),
        "stale_after":   (now + timedelta(minutes=90)).isoformat(),
    }
    tmp = SIGNAL_PATH.with_suffix(SIGNAL_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(SIGNAL_PATH)


def _slim_cluster(c: dict | None) -> dict | None:
    if not c:
        return None
    return {
        "score":       c.get("score"),
        "confidence":  c.get("confidence"),
        "direction":   c.get("direction"),
        "event_class": c.get("event_class"),
        "n":           c.get("n"),
        "top": {
            "headline": c.get("top", {}).get("headline"),
            "source":   c.get("top", {}).get("source"),
            "tier":     c.get("top", {}).get("tier"),
            "ts_seen":  c.get("top", {}).get("ts_seen"),
        } if c.get("top") else None,
        "headlines": [
            {
                "headline": h.get("headline"),
                "source":   h.get("source"),
                "tier":     h.get("tier"),
            } for h in (c.get("all") or [])
        ],
    }


# ── Alert formatting ─────────────────────────────────────────────────────────
_EVENT_CLASS_LABEL = {
    "geopolitical_deescalation": "Geopolitical de-escalation",
    "geopolitical_escalation":   "Geopolitical escalation",
    "monetary_dovish":           "Monetary policy — DOVISH",
    "monetary_hawkish":          "Monetary policy — HAWKISH",
    "oil_spike":                 "Oil price SPIKE",
    "oil_crash":                 "Oil price CRASH",
    "trade_escalation":          "Trade tensions ESCALATING",
    "trade_deescalation":        "Trade tensions easing",
    "financial_crisis":          "Financial / banking stress",
    "india_macro_positive":      "India macro POSITIVE",
    "india_macro_negative":      "India macro NEGATIVE",
    "corporate_india_megacap":   "Corporate India (NIFTY heavyweight)",
}

_READING_BLURB = {
    "geopolitical_deescalation": "Risk-on for EM equities; lower oil → bullish India",
    "geopolitical_escalation":   "Risk-off; higher oil/gold → bearish India CAD",
    "monetary_dovish":           "Liquidity supportive → bullish equities; INR mixed",
    "monetary_hawkish":          "Tighter financial conditions → bearish equities",
    "oil_spike":                 "Higher crude → India CAD/inflation pressure → bearish NIFTY",
    "oil_crash":                 "Lower crude → India macro tailwind → bullish",
    "trade_escalation":          "Risk-off for export-sensitive equities",
    "trade_deescalation":        "Risk-on; bullish for global equities",
    "financial_crisis":          "Contagion risk → bearish all risk assets",
    "india_macro_positive":      "Direct support for Indian equities",
    "india_macro_negative":      "Direct headwind for Indian equities",
    "corporate_india_megacap":   "NIFTY-heavyweight specific — direction set by news content",
}


def _esc(s: str) -> str:
    return _html_lib.escape(str(s or ""))


def _format_position_block(v3_state: dict, news_dir: int) -> str:
    n = v3_state["nifty"]
    b = v3_state["banknifty"]
    lines = ["📊 <b>v3 STATE:</b>"]

    def _runner_line(s: dict) -> str:
        if s["in_position"]:
            return (f"   • {s['instrument']}: <b>OPEN</b> {s['side']} {s['strike']} "
                    f"@ ₹{s['entry_price']:.2f}, ltp ₹{s['ltp']:.2f} "
                    f"({s['pnl_pct']:+.1f}%, ₹{s['pnl_inr']:+.0f})")
        sm = s.get("smoothed_score")
        if s.get("exited"):
            return (f"   • {s['instrument']}: closed today ({s['exit_reason']}) — "
                    f"no open position")
        if sm is None:
            return f"   • {s['instrument']}: no recent signal data"
        return f"   • {s['instrument']}: no position, smoothed_score={sm:+.2f}"

    lines.append(_runner_line(n))
    lines.append(_runner_line(b))
    lines.append("")
    lines.append("⚖️ <b>IMPACT:</b>")
    lines.append(f"   • NIFTY: {PC.impact_for(news_dir, n)}")
    lines.append(f"   • BANKNIFTY: {PC.impact_for(news_dir, b)}")
    return "\n".join(lines)


def format_alert(global_agg: dict, v3_state: dict) -> str:
    score = float(global_agg["score"])
    conf  = float(global_agg["confidence"])
    direction = +1 if score > 0 else (-1 if score < 0 else 0)
    label = "BULLISH" if direction > 0 else ("BEARISH" if direction < 0 else "NEUTRAL")

    top = global_agg.get("top_cluster") or {}
    top_h = top.get("top") or {}
    headline = top_h.get("headline", "—")
    source   = top_h.get("source",   "—")
    tier     = top_h.get("tier",     0.0)
    ev_cls   = top.get("event_class") or top_h.get("event_class") or "—"
    cls_lbl  = _EVENT_CLASS_LABEL.get(ev_cls, ev_cls)
    blurb    = _READING_BLURB.get(ev_cls, "")
    ts_seen  = top_h.get("ts_seen", "")
    try:
        ts_short = datetime.fromisoformat(ts_seen).strftime("%H:%M IST")
    except Exception:
        ts_short = "—"

    parts = []
    parts.append(f"🚨 <b>NEWS SIGNAL — {label}</b>  ({score:+.2f}, conf {conf:.2f})")
    parts.append("")
    parts.append(f"📰 <i>\"{_esc(headline)}\"</i>")
    parts.append(f"   {_esc(source)} · {ts_short} · tier={tier}")
    parts.append("")
    parts.append(f"🏷️  <b>{cls_lbl}</b>")
    if blurb:
        parts.append(f"💡 {_esc(blurb)}")
    parts.append("")
    parts.append(_format_position_block(v3_state, direction))

    # Corroborating items (if cluster has more than 1 headline).
    # Raw cluster carries them as `all`; slimmed clusters carry them as `headlines`.
    others_src = top.get("all") or top.get("headlines") or []
    others = [h for h in others_src if h.get("headline") != headline]
    if others:
        parts.append("")
        parts.append(f"🔗 <b>Corroborating ({len(others)}):</b>")
        for h in others[:4]:
            parts.append(f"   • {_esc(h.get('source','?'))}: \"{_esc(h.get('headline',''))[:90]}\"")

    parts.append("")
    parts.append(f"<i>Mode: alert-only (v3 trade logic untouched).</i>")
    return "\n".join(parts)


# ── Rate-cap helpers (May 2026 cleanup) ──────────────────────────────────────
def _now_ist() -> datetime:
    return datetime.now(IST)


def _alerted_records() -> list[dict]:
    """All records from alerted.json with normalised ts."""
    from . import dedup as _d
    a = _d._load(_d.ALERTED_PATH)
    out = []
    for ek, rec in a.items():
        if isinstance(rec, dict) and rec.get("ts"):
            try:
                out.append({"event_key": ek,
                            "ts": datetime.fromisoformat(rec["ts"]),
                            "direction": int(rec.get("direction", 0))})
            except Exception:
                continue
    return out


def _count_alerts_in_window(minutes: int | None = None,
                            today_only: bool = False) -> int:
    """Count alerts from alerted.json within last <minutes> or today (IST)."""
    recs = _alerted_records()
    now = _now_ist()
    if minutes is not None:
        cutoff = now - timedelta(minutes=minutes)
        return sum(1 for r in recs if r["ts"] >= cutoff)
    if today_only:
        today = now.date()
        return sum(1 for r in recs if r["ts"].date() == today)
    return len(recs)


def _theme_key(event_key: str) -> str | None:
    """First two anchor tokens (sorted) — used as a coarse theme guard."""
    from .normalize import ANCHOR_TOKENS
    tokens = [t for t in event_key.split("_") if t in ANCHOR_TOKENS]
    if not tokens:
        return None
    tokens = sorted(set(tokens))[:2]
    return "_".join(tokens)


def _theme_alerted_recently(theme: str, cooldown_min: int) -> bool:
    if not theme:
        return False
    from . import dedup as _d
    data = _d._load(THEME_ALERTED_PATH) if THEME_ALERTED_PATH.exists() else {}
    rec = data.get(theme)
    if not rec:
        return False
    try:
        ts = datetime.fromisoformat(rec.get("ts", ""))
    except Exception:
        return False
    return (_now_ist() - ts) < timedelta(minutes=cooldown_min)


def _mark_theme_alerted(theme: str) -> None:
    if not theme:
        return
    from . import dedup as _d
    THEME_ALERTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _d._load(THEME_ALERTED_PATH) if THEME_ALERTED_PATH.exists() else {}
    # Prune entries older than 24h
    cutoff = _now_ist() - timedelta(hours=24)
    data = {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("ts")
            and datetime.fromisoformat(v["ts"]) >= cutoff}
    data[theme] = {"ts": _now_ist().isoformat()}
    _d._save(THEME_ALERTED_PATH, data)


# ── Main entrypoint ──────────────────────────────────────────────────────────
def maybe_alert(global_agg: dict, v3_state: dict, force: bool = False) -> bool:
    """If thresholds are met and event_key not yet alerted, broadcast.

    Two paths fire an alert:
    * Standard:  |score| ≥ ALERT_SCORE_MIN AND confidence ≥ ALERT_CONF_MIN
                 (multi-source corroboration)
    * Tier-1 fast-path: top headline is Tier-1 AND its OWN |score| ≥ T1_SCORE_MIN
                 (single trusted wire reporting a strong event — fire NOW)

    Returns True if an alert was sent.
    """
    score_raw = float(global_agg.get("score", 0.0))
    score_abs = abs(score_raw)
    conf      = float(global_agg.get("confidence", 0.0))
    top       = global_agg.get("top_cluster") or {}
    top_h     = top.get("top") or {}
    top_tier  = float(top_h.get("tier", 0.0))
    top_score = abs(float(top_h.get("score", 0.0)))
    agg_dir   = +1 if score_raw > 0 else (-1 if score_raw < 0 else 0)

    standard_ok = (score_abs >= ALERT_SCORE_MIN and conf >= ALERT_CONF_MIN)
    tier1_ok    = (top_tier >= TIER1_MIN_TIER and top_score >= TIER1_FASTPATH_SCORE_MIN)

    if not force and not (standard_ok or tier1_ok):
        return False

    # Dry-run gate — set NEWS_DRY_RUN=1 to log alerts without sending Telegram.
    # Useful while iterating on keywords/thresholds. Still records to alerted.json
    # if `force=False` so dedup behaves the same as a real run.
    if os.environ.get("NEWS_DRY_RUN", "").lower() in ("1", "true", "yes"):
        log.info("DRY-RUN alert (would have sent): score=%+.2f conf=%.2f tier=%.1f item_score=%.2f",
                 float(global_agg.get("score", 0.0)), conf, top_tier, top_score)
        # Still mark alerted so dedup is consistent with prod behavior
        if not force:
            ek = (global_agg.get("top_cluster") or {}).get("event_key")
            if ek:
                dedup.mark_alerted(ek, direction=agg_dir)
        return False

    top = global_agg.get("top_cluster") or {}
    # event_key for the top cluster; we stored it as cluster["event_key"] in the
    # full cluster dict, but slim_cluster strips it. Fall back to anchor headline.
    event_key = top.get("event_key")
    if not event_key:
        # Reconstruct event_key from anchor headline (matches dedup.assign_cluster path)
        from . import normalize as N
        anchor = (top.get("top") or {}).get("headline", "")
        event_key = N.event_key(anchor) or N.headline_hash(anchor)[:16]

    # Anchor-word gate — _NO_ANCHOR_ marker means the event_key has no
    # recognisable topic anchor (probably a fragmented headline). Route to
    # digest so it isn't silently lost.
    from .normalize import NO_ANCHOR
    if event_key == NO_ANCHOR or event_key.startswith(NO_ANCHOR):
        if not force:
            try:
                from . import digest as _digest
                _digest.add({
                    "event_key": event_key,
                    "score":     score_raw,
                    "confidence": conf,
                    "direction": agg_dir,
                    "headline":  (top.get("top") or {}).get("headline", ""),
                    "source":    (top.get("top") or {}).get("source", ""),
                    "ts":        _now_ist().isoformat(),
                }, reason="no_anchor")
            except Exception as e:
                log.warning("digest.add (no_anchor) failed: %s", e)
        return False

    # Direction-aware suppression — if the same event_key already alerted but
    # the news direction has FLIPPED since, allow the re-alert.
    if not force and dedup.already_alerted(event_key, direction=agg_dir):
        return False

    # ── Rate-cap gates (route capped items to EOD digest) ─────────────────
    def _to_digest(reason: str) -> bool:
        try:
            from . import digest as _digest
            _digest.add({
                "event_key": event_key,
                "score":     float(global_agg.get("score", 0.0)),
                "confidence": conf,
                "direction": agg_dir,
                "headline":  (top.get("top") or {}).get("headline", ""),
                "source":    (top.get("top") or {}).get("source", ""),
                "ts":        _now_ist().isoformat(),
            }, reason=reason)
        except Exception as e:
            log.warning("digest.add failed: %s", e)
        log.info("capped:%s event_key=%s score=%+.2f dir=%+d → digest",
                 reason, event_key, score_raw, agg_dir)
        # Still mark alerted so we don't keep evaluating the same event_key
        dedup.mark_alerted(event_key, direction=agg_dir)
        return False

    if not force:
        # Per-day cap (applies to BOTH paths — even tier-1 fastpath)
        if _count_alerts_in_window(today_only=True) >= NEWS_MAX_PER_DAY:
            return _to_digest("day")
        # Per-hour cap (tier-1 fastpath exempt — flash events still fire)
        if not tier1_ok and _count_alerts_in_window(minutes=60) >= NEWS_MAX_PER_HOUR:
            return _to_digest("hour")
        # Theme cooldown
        theme = _theme_key(event_key)
        if theme and _theme_alerted_recently(theme, NEWS_THEME_COOLDOWN_MIN):
            return _to_digest("theme")

    token, chats = _load_telegram_config()
    if not token or not chats:
        log.warning("Telegram disabled — would have alerted on event_key=%s", event_key)
        return False

    msg = format_alert(global_agg, v3_state)
    n = _broadcast(token, chats, msg)
    if n > 0 and not force:
        dedup.mark_alerted(event_key, direction=agg_dir)
        theme = _theme_key(event_key)
        if theme:
            _mark_theme_alerted(theme)
        log.info("Alert sent to %d chat(s) for event_key=%s dir=%+d theme=%s",
                 n, event_key, agg_dir, theme or "—")
        return True
    return False


# ── CLI: dry-run testing ─────────────────────────────────────────────────────
def _build_test_agg() -> dict:
    """Synthetic high-impact alert for --test."""
    from . import scorer
    headlines = [
        ("Trump announces US-Iran peace deal, sanctions lifted",                "Axios",     1.0),
        ("US, Iran reach agreement; sanctions to be removed",                   "Reuters",   1.0),
        ("Markets rally on US-Iran breakthrough",                               "CNBC",      0.7),
    ]
    scored = []
    for h, s, t in headlines:
        item = scorer.score_headline(h, s, t, datetime.now(IST))
        item["event_key"] = "iran_peace_sanctions_us_deal"
        scored.append(item)
    cluster = aggregate_cluster(scored)
    cluster["event_key"] = "iran_peace_sanctions_us_deal"
    return aggregate_global([cluster])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Send a synthetic alert")
    p.add_argument("--dry",  action="store_true", help="Print message but don't send")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.test:
        agg = _build_test_agg()
        v3  = PC.read_v3_state()
        update_signal_file(agg)
        msg = format_alert(agg, v3)
        if args.dry:
            print(msg)
        else:
            ok = maybe_alert(agg, v3, force=True)
            print(f"alert sent: {ok}")
            if not ok:
                print("--- message would have been: ---")
                print(msg)
    else:
        p.print_help()
