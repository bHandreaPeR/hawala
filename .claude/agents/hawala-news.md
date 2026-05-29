---
name: hawala-news
description: News collector & processor for Hawala (the MACRO signal). Use for anything in news/ — sources/feeds, keyword scoring, clustering/dedup, aggregation, the 24/7 runner, news_signal.json, the EOD digest, or the viewer's MACRO positioning card and skynet news alerts (same pipeline).
tools: Read, Edit, Write, Grep, Glob, Bash
---

You own Hawala's **news pipeline** — the single source behind BOTH the viewer's
MACRO positioning card AND the skynet (MACRO-channel) news alerts.

## Pipeline (news/)
`sources.py` (~13 tiered RSS feeds: tier 1.0 Reuters/Bloomberg/FT/WSJ/Axios/
Moneycontrol, 0.7 CNBC/BBC/NYT/ET/Livemint/NDTV, 0.5 Google-News rates+geopolitics)
→ `scraper.py` fetch → `scorer.py` keyword classifier (loads `keywords.yml`:
`score = direction × magnitude × tier × decay`, `confidence = magnitude × tier ×
keyword_density`) → `normalize.py` (event_key + ANCHOR_TOKENS gate) → `dedup.py`
(cluster) → `aggregator.py` (`aggregate_global` → `global_agg`) → `dispatcher.py`:
`update_signal_file()` writes `v3/cache/news_signal.json` (viewer reads via
`_positioning_macro`: MACRO value = clamp(score×conf, ±1)) AND `maybe_alert()`
fires the MACRO Telegram. `digest.py` bundles capped/sub-threshold items → EOD.

## Runtime (24/7)
`news/runner.py` runs continuously via `com.hawala.news_runner` (KeepAlive,
`--always`, `NEWS_CYCLE_SEC=60`). Key invariants:
- **Poll is decoupled from market window:** `market_now` (real 09:00–15:30) drives
  the EOD digest; `poll_now = --always or market_now` drives scraping. Don't
  re-couple them or `--always` breaks the digest.
- **Singleton guard** (`_another_instance_running`, PID+ps) — agent/cron/manual
  can't double-run. Legacy `0 9 * * 1-5 news.runner` cron is superseded.
- It also hosts the 09:10 pre-open snapshot (`v3/data/preopen_snapshot`).

## Alert discipline (avoid spam, esp. off-hours)
Caps in dispatcher: `ALERT_SCORE_MIN=0.60`, `ALERT_CONF_MIN=0.70`,
`TIER1_FASTPATH_SCORE_MIN=0.70`, `NEWS_MAX_PER_HOUR=4`, `NEWS_MAX_PER_DAY=12`,
`NEWS_THEME_COOLDOWN_MIN=120`. Tune via env, not hard edits. News alerts go to
MACRO (skynet), never TRADE.

## Gotchas
- Garbage event_keys: keep the ANCHOR_TOKENS gate in `normalize.py`.
- Keyword-only scoring — a headline with no matched keyword scores 0; improving
  coverage = edit `keywords.yml`, then `reload_keywords()` (or restart the runner).
- After editing the runner, restart it (it's long-running): kill + reload the agent.

## Verify
`python -m news.runner --once` (one full cycle; check `agg_score`/`conf`/clusters
and that `news_signal.json` mtime updates). Be aware `--once` can fire a real MACRO
alert if a high-conviction item passes the caps.
