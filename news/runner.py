"""News runner — main loop.

Lifecycle:
  09:00 IST  — start polling
  09:00..15:30 — every CYCLE_SEC, scrape → score → cluster → aggregate
                → write v3/cache/news_signal.json → maybe send Telegram alert
  15:30 IST  — sleep until next trading day's 09:00

Usage:
    python -m news.runner
    python -m news.runner --once     # run a single cycle and exit
    python -m news.runner --pid-file news/news_runner.pid

Logs to news/news_runner.log.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal as _signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import dedup
from . import dispatcher
from . import sentiment as _sentiment
from . import position_context as PC
from . import scorer
from .aggregator import aggregate_cluster, aggregate_global
from .dedup import IST
from .scraper import fetch_all


CYCLE_SEC = int(os.environ.get("NEWS_CYCLE_SEC", "30"))
ACTIVE_WINDOW_SEC = int(os.environ.get("NEWS_ACTIVE_WINDOW_SEC", "120"))

MARKET_OPEN_H,  MARKET_OPEN_M  = 9,  0
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "news_bot" / "news_runner.log"
PID_PATH = Path(__file__).parent / "news_runner.pid"

log = logging.getLogger("news.runner")


def _now() -> datetime:
    return datetime.now(IST)


def _ts_after(ts_iso: str | None, cutoff: datetime) -> bool:
    """True if `ts_iso` parses to a datetime ≥ cutoff."""
    if not ts_iso:
        return False
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt >= cutoff
    except Exception:
        return False


def _is_market_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_t = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_t <= now <= close_t


def _seconds_to_next_open(now: datetime) -> float:
    target = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


def cycle_once() -> dict:
    """Single end-to-end cycle. Returns the global aggregate."""
    t0 = time.monotonic()
    new_items = fetch_all(dedup_layer1=True)
    t_fetch = time.monotonic() - t0

    # Score every new item, then assign to a cluster
    n_scored = 0
    for it in new_items:
        s = scorer.score_headline(
            it["headline"], it["source"], it["tier"],
            ts_seen=datetime.fromisoformat(it["ts_seen"]) if isinstance(it["ts_seen"], str) else None,
        )
        # Carry url through
        s["url"] = it.get("url", "")
        if s["event_class"]:
            n_scored += 1
            # Feed the rolling 24h MACRO sentiment log (separate from the
            # 3-min alert pulse). Stamp the item's ts so decay is age-correct.
            s["ts_seen"] = it.get("ts_seen")
            _sentiment.record(s)
        ek, _is_new = dedup.assign_cluster(it["headline"])
        dedup.upsert_cluster(ek, s)

    # Aggregate every active cluster.
    # A cluster contributes to the LIVE signal only if it has at least one
    # headline seen within ACTIVE_WINDOW_SEC. Older clusters stay in storage
    # for dedup/corroboration but don't drive new alerts.
    now = datetime.now(IST)
    cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SEC)
    clusters_raw = dedup.active_clusters()
    cluster_aggs: list[dict] = []
    for ek, c in clusters_raw.items():
        fresh = [h for h in c.get("headlines", [])
                 if _ts_after(h.get("ts_seen"), cutoff)]
        if not fresh:
            continue
        # Aggregate across the WHOLE cluster (so corroboration boost reflects
        # all sources that have reported, even if some are now stale), but
        # only emit the cluster if it has fresh headlines.
        agg = aggregate_cluster(c.get("headlines", []))
        agg["event_key"]   = ek
        agg["n_fresh"]     = len(fresh)
        cluster_aggs.append(agg)

    global_agg = aggregate_global(cluster_aggs)
    # Stamp event_key onto top_cluster so dispatcher can read it cleanly
    if global_agg.get("top_cluster"):
        for c in cluster_aggs:
            if c is global_agg["top_cluster"]:
                c["event_key"] = c.get("event_key")
                break

    # Rolling 24h sentiment for the viewer MACRO card (stable mood, recency-
    # weighted). Written alongside the pulse; the alert path is untouched.
    sentiment_agg = _sentiment.rolling(now)
    dispatcher.update_signal_file(global_agg, sentiment=sentiment_agg)

    v3_state = PC.read_v3_state()
    sent = dispatcher.maybe_alert(global_agg, v3_state)

    elapsed = time.monotonic() - t0
    log.info(
        "cycle: new=%d scored=%d clusters=%d agg_score=%+.2f conf=%.2f alert=%s "
        "(fetch=%.1fs total=%.1fs)",
        len(new_items), n_scored, len(cluster_aggs),
        global_agg["score"], global_agg["confidence"], sent,
        t_fetch, elapsed,
    )
    return global_agg


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _another_instance_running() -> bool:
    """True if a DIFFERENT live `news.runner` process owns the PID file."""
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
    except Exception:
        return False
    if pid == os.getpid():
        return False
    try:
        out = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='],
                                      text=True, timeout=5)
        return 'news.runner' in out          # alive AND actually our runner
    except Exception:
        return False                          # dead/recycled PID → free to start


_running = True


def _handle_sigterm(_signum, _frame):
    global _running
    log.info("Received signal — shutting down after current cycle")
    _running = False


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once",     action="store_true", help="Run one cycle and exit")
    p.add_argument("--always",   action="store_true", help="Ignore market-hours gate")
    args = p.parse_args()

    _setup_logging()

    # Singleton guard: with a 24/7 KeepAlive agent + the legacy 09:00 cron +
    # ad-hoc manual starts, multiple instances could otherwise run at once
    # (double alerts, racing state writes). If a live news.runner already owns
    # the PID file, this instance exits cleanly.
    if not args.once and _another_instance_running():
        log.info("another news.runner is already alive — exiting (singleton).")
        return

    # Write PID
    try:
        PID_PATH.write_text(str(os.getpid()))
    except Exception:
        pass

    _signal.signal(_signal.SIGTERM, _handle_sigterm)
    _signal.signal(_signal.SIGINT,  _handle_sigterm)

    if args.once:
        cycle_once()
        return

    log.info("News runner starting (cycle=%ds, market_hours=%s)",
             CYCLE_SEC, "OFF" if args.always else "9:00–15:30 IST")

    was_in_market = False   # tracks transition for EOD digest emit
    preopen_done_for = None # date the pre-open snapshot was last captured

    while _running:
        now = _now()
        # Decouple "should we poll" from "is the market open":
        #   market_now → real 09:00-15:30 session (drives the EOD digest)
        #   poll_now   → whether to scrape this cycle (--always = 24/7)
        market_now = _is_market_window(now)
        poll_now   = args.always or market_now

        # Pre-open auction snapshot — once per day, when clock crosses 09:10.
        # news.runner is the only process alive before the v3 runners start
        # at 09:12, so it hosts this capture (no extra cron). Writes
        # v3/cache/preopen_signal.json for the runners to read at startup.
        try:
            if (preopen_done_for != now.date()
                    and now.hour == 9 and now.minute >= 10
                    and now.weekday() < 5):
                from v3.data.preopen_snapshot import capture as _preopen_capture
                _preopen_capture()
                preopen_done_for = now.date()
                log.info("Pre-open snapshot captured for %s", now.date())
        except Exception as e:
            log.exception("Pre-open snapshot failed: %s", e)

        # Edge: in-market → out-of-market (market just closed) → emit EOD digest
        if was_in_market and not market_now:
            try:
                from news.digest import emit_eod_digest
                emit_eod_digest()
            except Exception as e:
                log.exception("emit_eod_digest failed: %s", e)
        was_in_market = market_now

        if not poll_now:
            wait = _seconds_to_next_open(now)
            log.info("Outside market hours — sleeping %.0fs until next open", wait)
            # Sleep in chunks so SIGTERM is responsive
            slept = 0.0
            while _running and slept < wait:
                time.sleep(min(60.0, wait - slept))
                slept += 60.0
            continue
        try:
            cycle_once()
        except Exception as e:
            log.exception("Cycle failed: %s", e)
        # Sleep until next cycle
        slept = 0.0
        while _running and slept < CYCLE_SEC:
            time.sleep(min(5.0, CYCLE_SEC - slept))
            slept += 5.0

    # On clean shutdown, also try to flush the digest (covers SIGTERM after 15:30)
    try:
        from news.digest import emit_eod_digest, pending_count
        if pending_count() > 0:
            emit_eod_digest()
    except Exception as e:
        log.exception("Shutdown digest emit failed: %s", e)

    log.info("News runner stopped.")


if __name__ == "__main__":
    main()
