# Hawala — Master Context

> **Single source of truth.** Read this BEFORE adding any new feature, refactor, or
> investigation. It tells you what was built, what is in production, what was
> rejected and why, and where everything lives. Keep it in sync as you go.
>
> Last updated: 2026-05-23 — Mac→Windows migration complete + footprint pipeline +
> re-entry cooldown + VP intraday executor + Mac→Windows SSH heartbeat.

---

## 0. Where production runs (post 2026-05-23 migration)

**Production runs on Windows.** Mac is dev-only now.

| Machine | Role |
|---|---|
| **Windows (LAPTOP-JJOUAFCP, 192.168.0.6)** | All 17 scheduled tasks fire here. The "production box". Anaconda at `D:\anaconda3\`, repo at `D:\Hawala\Hawala v2`. |
| **Mac** | Dev only. Project Iris crons still run. Hawala SSH heartbeat (launchd `com.hawala.winheartbeat`) checks Windows every 10 min → MACRO bot alert if DOWN ≥20 min. |

Mac→Windows over SSH (`hawala-win` alias, ed25519 key, LocalForward 8765 for the
live viewer). Single `main` branch on `bHandreaPeR/hawala` on GitHub.

17 scheduled tasks live in `windows/scheduled_tasks/*.xml`. Regenerate via
`python windows/generate_tasks.py`. Re-import via `windows/IMPORT_TASKS.ps1`
(Administrator PowerShell).

See: `docs/WINDOWS_MIGRATION.md` for full migration plan + rollback.

---

## 1. What this repo actually is

Two parallel stacks coexist in this directory:

| Stack | Status | What it does |
|---|---|---|
| **v3** (`v3/`, `news/`) | **PRODUCTION** — runs from cron daily | OI/FII-based intraday signal engine for NIFTY + BANKNIFTY options. News scraper feeds context. |
| **v2** (`strategies/`, `backtest/`, `alerts/`, `bots/`, `reports/`, `research/`) | RESEARCH — not in cron | VP-Trail-Swing + ORB + OPT_ORB backtest stack. Telegram daemons exist but are not currently scheduled. |

**Anything you find in cron is v3 + the daily/weekly report scripts. Everything
else is research, archived, or one-off.**

## 2. Production cron (authoritative)

```cron
# Daily newsletter PDF — sent to MACRO bot only
32 7 * * 1-5  ... python3 run_daily_report.py >> /tmp/hawala_report.log 2>&1

# News runner — starts 09:00 IST, kills 03:30 next day
0 9 * * 1-5   ... nohup ... -m news.runner > /dev/null 2>&1 &

# v3 live runners — start 09:12 IST
12 9 * * 1-5  ... v3/live/runner_nifty.py & v3/live/runner_banknifty.py &

# Kill all live runners
30 3 * * 1-5  pkill -f "runner_nifty.py"; pkill -f "runner_banknifty.py"; pkill -f "alert_runner.py"; pkill -f "news\.runner"

# EOD candle / option-OI / bhavcopy fetch
30 16 * * 1-5 ... bash v3/scripts/daily_fetch.sh

# Weekly cache backfill
30 2 * * 0    ... bash v3/scripts/weekly_backfill.sh

# Friday weekly report (TRADE bot — trade summary)
0 18 * * 5    ... python3 run_weekly_report.py
```

`alert_runner.py` is **NOT** in cron — replaced by the v3 runners. It's kept in
`archived/v2_legacy/` only because the pkill line guards against accidental
manual invocation.

## 3. What runs where (live system, May 2026)

```
07:32 IST   run_daily_report.py        → MACRO bot   (Newsletter PDF only, no message)
09:00       news/runner.py             → MACRO bot   (ad-hoc news event alerts — routed to MACRO from May 14)
09:12       v3/live/runner_nifty.py    → TRADE bot   (NIFTY option entry/exit)
09:12       v3/live/runner_banknifty.py→ TRADE bot   (BANKNIFTY option entry/exit)
15:30       (runners idle until next day; news runner sleeps)
16:35       v3/scripts/daily_fetch.sh  → fills v3/cache/
18:00 Fri   run_weekly_report.py       → TRADE bot   (week's trade summary)
03:30       pkill                      (defensive)
02:30 Sun   v3/scripts/weekly_backfill.sh → v3/cache/
```

## 4. Two Telegram channels

| Bot | Token env var | Used for |
|---|---|---|
| **TRADE** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_IDS` | v3 entries/exits, news event alerts, weekly trade summary |
| **MACRO** | `TELEGRAM_BOT_TOKEN_MACRO` + `TELEGRAM_CHAT_IDS_MACRO` | Daily newsletter PDF (07:32). NOTHING ELSE currently. |

`bots/macro_bot.py` exists with `--mode {premarket,midday,eod,daemon}` but is
NOT cron-scheduled. If activated, briefs would also go to MACRO.

## 5. Directory map — where everything lives now

```
hawala-v2/
├── README.md, ARCHITECTURE.md, DEPLOYMENT.md, CONTEXT.md (this file)
├── token.env                       — credentials
├── config.py                       — instrument config (lot sizes, margins)
├── requirements.txt
│
├── run_daily_report.py             — CRON 07:32 → Newsletter PDF → MACRO
├── run_weekly_report.py            — CRON Fri 18:00 → trade summary → TRADE
│
├── v3/                             ★ ACTIVE PRODUCTION
│   ├── live/                       runner_nifty, runner_banknifty, scanner, hero_zero
│   ├── signals/                    engine, classifiers (FII/DII), max_pain, expiry_mode
│   ├── data/                       fetchers (1m, option_oi, bhavcopy, fii)
│   ├── backtest/                   v3 backtest harness
│   ├── scripts/                    daily_fetch.sh, weekly_backfill.sh, morning_fetch.sh
│   ├── cache/                      pkl + csv data caches (gitignored heavy)
│   └── signals/                    classifier definitions
│
├── news/                           ★ ACTIVE PRODUCTION (news monitor)
│   ├── runner.py                   main loop
│   ├── scraper.py / aggregator.py  RSS pulls + scoring
│   ├── scorer.py / dedup.py        signal generation
│   ├── dispatcher.py               TRADE-bot ad-hoc alerts
│   ├── normalize.py / sources.py   pipelines
│   ├── keywords.yml                trigger-word config
│   ├── monitor/, state/            runtime state (alerted.json, seen.json)
│   └── backtest/                   retro-news + event attribution
│
├── data/                           — v2 fetchers (still used by daily-report)
│   ├── fetch_report_data.py        — IMPORTED by run_daily_report
│   ├── futures_fetch.py / options_fetch.py / fii_fetch.py
│   ├── contract_resolver.py
│   └── cache_15m/                  — v2 15m cache (used by research)
│
├── alerts/
│   ├── telegram.py                 — IMPORTED by run_daily_report.send_document
│   └── option_flow_daemon.py       — 5-sec strike-level OI flow tracker → MACRO
│
├── daily_report/                   — alt path used by gen_report.build_pdf fallback
├── gen_html_report.py              — IMPORTED by run_daily_report
├── gen_report.py                   — IMPORTED by run_daily_report (PDF fallback)
│
├── strategies/                     — v2 strategy code (research)
│   ├── orb.py, options_orb.py, vp_trailing_swing.py, volume_profile.py
│   ├── vwap_reversion.py, candlestick.py, expiry_spread.py, iron_condor.py
│   ├── last_hour.py, narrow_range_breakout.py, gap_fill.py, patterns.py
│   └── vwap_slope_momentum.py
│
├── backtest/                       — v2 backtest infra
│   ├── compounding_engine.py       — risk caps (2% per-trade, 5% daily halt)
│   ├── combiner.py / engine.py / walk_forward.py / options_layer.py
│
├── bots/
│   └── macro_bot.py                — not in cron; smoke-test ready
│
├── reports/                        — weekly Excel + multi-strategy explorer refresh
│   ├── build_weekly_report.py      — produces weekly_backtest.xlsx
│   ├── refresh_weekend.sh          — Saturday 06:00 hook
│   └── weekly_backtest.xlsx
│
├── research/                       — exploratory, interactive
│   ├── trade_explorer.py           — multi-strategy HTML viewer builder
│   ├── trade_explorer.html         — 953-trade interactive view
│   └── signal_ic.py
│
├── trade_logs/                     — backtest outputs (per-strategy, gitignored heavy)
│
├── adhoc/                          ⚙ NOT IN CRON
│   ├── run_canonical.py / run_baseline.py / run_full_backtest.py
│   ├── run_2026_oos.py / run_next_steps.py / run_candlestick_backtest.py
│   ├── run_expiry_spread_backtest.py / run_sensex_sweep.py
│   ├── analyse_signals.py / iterate.py / set_tokens.py / test_groww_expired.py
│   └── signal_schema.json
│
├── ops/                            ⚙ MAINTENANCE
│   ├── kill_runners.sh / restart_runners.sh
│   └── push_to_github.sh / setup_hawala_v3_git.sh
│
├── data_dumps/                     📦 LARGE FILES — NOT in code paths
│   ├── newsletters/                — Newsletter <DDth Month YY>.pdf (NEW format)
│   ├── newsletters_archive/        — pre-rename market_report_*.{pdf,html}
│   ├── signals/                    — market_signal_<date>.json (run_daily_report output)
│   ├── nse_bhavcopy/               — nsccl.* archive files
│   ├── fii_history/                — fii_stats_*.xls
│   ├── reference/                  — Groww API PDFs, ORB Excel, screenshots
│   ├── trade_logs_archive/         — trade_log_*.csv (loose top-level dumps)
│   ├── combine_oi_archive/         — combineoi_04052026/* + zip
│   └── mar2025/                    — mar-2025.xlsx, Mar_2025.zip
│
├── logs/                           📜 CENTRAL LOG HUB
│   ├── trade_bot/                  — v3 live runners, alert_runner historical
│   ├── macro_bot/                  — daily report run logs (going forward)
│   ├── news_bot/                   — news_runner.log
│   └── reports/                    — weekly backfill, weekly report
│
└── archived/                       🪦 RETIRED
    ├── v2_legacy/                  alert_runner.py, vp_live_daemon.py, vp_signal_alert.py
    ├── notebook_cells/             cell_1…cell_9 (notebook export from April)
    ├── notebooks_consolidated/     Hawala 2.ipynb, Tester.ipynb, _consolidated.ipynb
    └── scratch/                    Untitled.ipynb, scratchpad.html, "-f", token.env.rtf
```

## 6. Build history — what was done, when, why

Chronological summary of significant work. **Append new entries at the bottom.**

### April 2026 — v2 research stack (PARTIALLY DEPLOYED, NOW RESEARCH-ONLY)

- ORB futures gap-fill on BANKNIFTY: 50–100 pt gap band, Tue/Wed/Fri only.
  41 trades, 70.7% WR, +₹3.67L over 4½ years. Active backtest result.
  Source: `strategies/orb.py`.
- VWAP_REV BANKNIFTY: built, then **dropped** after 2025 OOS collapse
  (-₹1.05L, WR 54→36). Code preserved at `strategies/vwap_reversion.py` but
  not imported by canonical/baseline runners. Trade logs archived.
- Candlestick / expiry-spread / iron-condor / narrow-range / last-hour
  experiments: built, all rejected. `strategies/{candlestick,expiry_spread,
  iron_condor,narrow_range_breakout,last_hour}.py` retained for reference.

### Late April — Volume Profile family

- `strategies/volume_profile.py` — primitive (front-month rolling profile,
  VAH/VAL/POC, regime shift detection). Used by VP-Trail.
- `strategies/vp_trailing_swing.py` — canonical fade strategy. Pierce of 70%
  VA + reversal candle, chandelier trail, EOD-profitable carry up to 3 days.
  Per-instrument tuning in `adhoc/run_canonical.py:CANONICAL_PARAMS`.
- Realistic slippage retrofit: BN 30 / NIFTY 10 / SENSEX 20 pt per leg,
  applied symmetrically. Reduced pre-slippage +₹4.96L → +₹0.44L. Honest
  number; small but real edge.
- Apr-May forensic-driven filters added (BANKNIFTY only):
  trend filter (block fades against 20D return >±2%),
  block re-entry after BREAKEVEN (one BE = day done),
  early-cut at 14:30 if pnl < -80 pts,
  daily-loss-limit tightened 600→300 pt.
  Result: BN P&L -₹57k → +₹29k; combined 1-lot canonical now +₹1.31L.
  NIFTY/SENSEX tested with same filters: each lost ~₹70k of edge — kept
  WITHOUT filters per per-instrument logic.

### Late April — Compounding engine + risk caps

- `backtest/compounding_engine.py`: added per-trade 2% loss cap and 5% daily
  equity halt (skips trades for the rest of the day). Both opt-in via params.
- Tracked separately in summary: `halts_daily`, `risk_capped`.

### Early May — Two-bot Telegram split

- TRADE bot reserved for entries/exits/P&L (existing).
- MACRO bot added for briefs only. `bots/macro_bot.py` with three modes
  (premarket/midday/eod) plus a continuous daemon. **Not currently cron-scheduled.**
- `alerts/vp_live_daemon.py` built to fire VP-Trail entries to TRADE bot.
  **Also not currently cron-scheduled.**

### Early May — Daily report production

- `run_daily_report.py` is the live morning brief (07:32 IST cron).
- News scraper (`data/fetch_report_data.py:_fetch_news`) pulls from **12
  trust-whitelisted RSS feeds** (BBC, CNBC, NY Times, ET, Livemint, Hindu
  BusinessLine, Zerodha Pulse, plus 5 domain-filtered Google News queries
  restricted to Reuters/Bloomberg/FT/WSJ/AP only) and ships into the PDF.
- **Output rename (May 10):** PDF now named `Newsletter <DDth Month YY>.pdf`
  (e.g. `Newsletter_13th May 26.pdf`). Lands in `data_dumps/newsletters/`.
- **Routing change (May 10):** sent to MACRO bot only (was TRADE bot). No
  summary text; PDF only, no caption.

### v3 — production OI/FII engine (separate development line)

- `v3/signals/engine.py` consumes per-minute option chain + spot + futures
  → produces directional score every minute via SignalSmoother.
- FII/DII classifiers in `v3/signals/fii_dii_classifier*.py`. Multiple
  variants tested; per-instrument winner cached at
  `v3/cache/fii_dii_thresholds_*.json`.
- Live runners (`v3/live/runner_{nifty,banknifty}.py`) implement:
  - 09:15 auth + minute-poll start
  - 09:15–10:15 OI history accumulation
  - 10:15+ first |score|>threshold → ATM CE/PE buy
  - 15:20 forced exit
- News integration: `news.dispatcher` posts ad-hoc alerts to TRADE channel
  on velocity events; `news/backtest/event_attribution.py` measures their
  predictive value.

### May 15 — Option-flow tracker (retail Bookmap analog)

Built strike-level option-OI flow classifier as the closest retail-Indian-
market approximation to Bookmap's Cumulative Volume Delta. Bookmap itself
needs TBT (tick-by-tick aggressor) data that isn't available to retail in
India — but the OI-quadrant classification per strike at 5-second cadence
yields a similar "who's positioning, on which side" view.

Three new artefacts:

1. **`v3/signals/option_flow.py`** — pure-compute classifier. Per strike
   per snapshot pair: classify CE/PE into one of 8 positioning states
   (long_call_build, short_call_cover, etc.), weight by ATM-proximity ×
   cash value of ΔOI, aggregate into signed net flow. Cumulative through
   the session = synthetic CVD analog.

2. **`alerts/option_flow_daemon.py`** — polls option chain every 5 sec
   during market hours for NIFTY + BANKNIFTY. Writes
   `v3/cache/option_flow_<inst>.json` (consumed by v3 runners) and sends
   Telegram alerts to MACRO channel on three extreme conditions:
       'flip'      — CVD crosses zero with conviction
       'sustained' — 5 consecutive same-direction snapshots
       'z_high'    — single-snapshot z-score > 2.0
   Rate-capped: 1 alert per (instrument, direction) per 15 min.

3. **v3 runner confluence hook (opt-in)** — both
   `runner_nifty.py` and `runner_banknifty.py` now read the flow file
   when `V3_REQUIRE_OPTION_FLOW_CONFIRM=1` and add a Filter-F6 veto:
   if v3's signal direction disagrees with the session CVD direction,
   suppress the entry. **Default OFF** — measure correlation for 2-3
   weeks before enabling.

To start (manual, or via cron):

```sh
nohup caffeinate -i python3 -m alerts.option_flow_daemon --mode daemon \
    > /dev/null 2>&1 &
```

Cron entry (add to 09:12 IST block):

```cron
12 9 * * 1-5 cd "/path" && nohup caffeinate -i /opt/anaconda3/bin/python3 -m alerts.option_flow_daemon --mode daemon > /dev/null 2>&1 &
```

Also add to defensive `pkill` line:

```cron
30 3 * * 1-5 ... ; pkill -f "alerts.option_flow_daemon"
```

### May 14-15 — News pipeline cleanup

Three rounds of work on `news/`:

1. **Routing fix (May 14)** — `news/dispatcher.py` was reading
   `TELEGRAM_BOT_TOKEN` (TRADE bot) for news alerts. Changed to prefer
   `TELEGRAM_BOT_TOKEN_MACRO` with fallback. TRADE channel now reserved
   strictly for entries/exits/P&L.

2. **Noise reduction (May 14)** — alerts/day was 40–45 and felt spammy.
   Added five layers:
   - Raised gates: `NEWS_ALERT_SCORE_MIN` 0.4→0.60, `NEWS_ALERT_CONF_MIN`
     0.6→0.70, `TIER1_FASTPATH_SCORE_MIN` 0.5→0.70
   - Per-hour cap (4), per-day cap (12), theme-cooldown (120 min) added
     to `dispatcher.maybe_alert`. State in `news/state/theme_alerted.json`.
   - Anchor-token gate in `normalize.event_key()` — keys without a token
     from `ANCHOR_TOKENS` (97 words) return `_NO_ANCHOR_` sentinel.
     Compound-token preserver (`S&P`, `L&T`, `M&A`) so well-known
     compounds survive tokenization.
   - EOD digest (`news/digest.py`) — items hitting gates/caps land here
     instead of being silently dropped, emitted as one MACRO message at
     market close.
   - Expected outcome: ~10 live alerts/day + 1 digest message.

3. **Source expansion + trust cleanup (May 15)** — feed list grew from
   32 → 47 then trimmed to 44:
   - **Added 11 feeds**: Zerodha Pulse, BSE filings, SEBI press, CNBC
     TV18, Hindu BusinessLine + economy, and 5 X-handles via Nitter
     (NSEIndia, BSEIndia, RBI, CNBCTV18Live, LiveSquawk).
   - **Removed 3 feeds for partisan/fake-news risk**: Truth Social
     (Trump posts), broad GN: breaking-30m and GN: india-30m searches
     (clickbait magnets).
   - **Tightened 4 feeds**: GN topical searches (rates/geopolitics/oil/
     india) now use `site:` filters to restrict to Reuters/Bloomberg/FT/
     WSJ/AP/CNBC/BBC and trusted Indian wires. Tier raised 0.4 → 0.5.
   - Applied the same cleanup to `data/fetch_report_data.py:_NEWS_FEEDS`
     (the newsletter PDF feed list) — went from 11 mixed-trust feeds to
     12 trust-whitelisted feeds with `site:` filters.
   - Kept Al Jazeera + NDTV Profit per user decision (Al Jazeera is
     state-funded with Middle East slant but editorially professional;
     NDTV Profit's Adani ownership flagged but retained).

### May 10 — Repo consolidation (this commit)

- Created `archived/`, `adhoc/`, `data_dumps/`, `logs/`, `ops/` top-level dirs.
- Moved 68+ historic files to `data_dumps/` (PDFs, HTMLs, zips, xls, csvs).
- Moved 13 adhoc scripts to `adhoc/` (research runners, smoke tests).
- Archived dead notebook cells, Untitled notebooks, scratchpad.
- Moved alert_runner.py + vp_live_daemon.py + vp_signal_alert.py to
  `archived/v2_legacy/` — they were already not in cron.
- Centralised log paths:
  - `news/runner.py` → `logs/news_bot/news_runner.log`
  - `v3/live/runner_nifty.py` → `logs/trade_bot/runner_nifty.log`
  - `v3/live/runner_banknifty.py` → `logs/trade_bot/runner_banknifty.log`
  - Existing log files copied as snapshots into `logs/` subdirs.
- Wrote this CONTEXT.md as the master reference.

## 6b. News source policy (May 2026)

Authoritative rules for what can appear in `news/sources.py:FEEDS` and
`data/fetch_report_data.py:_NEWS_FEEDS`:

**Allowed:**
- Wire services with editorial standards (Reuters, Bloomberg, AP, Axios,
  ForexLive)
- Broadsheet papers (FT, WSJ, NY Times, BBC, Hindu BusinessLine, ET,
  Livemint)
- Primary government / regulator sources (Fed press, RBI, SEBI, NSE
  filings, BSE filings)
- Official institutional X/Twitter accounts (@NSEIndia, @BSEIndia, @RBI)
- Headline-mirror X/Twitter accounts that re-tweet wire content without
  editorializing (@DeItaone, @FirstSquawk, @LiveSquawk, @CNBCTV18Live)
- Curated aggregators that source from the above (Zerodha Pulse)
- Google News searches **only when** restricted with `site:` filters to
  the above list

**Disallowed:**
- Personal-brand social media (Truth Social — Trump posts directly, plus
  any future similar feeds)
- Clickbait magnets (`q=breaking OR exclusive` style queries)
- Partisan blogs of any political alignment
- Unverified aggregators (Zerohedge, retail-trader sentiment accounts)
- Crypto-only media (low signal-to-noise for India equity)

**Borderline kept (with caveats):**
- Al Jazeera — state-funded, Middle East perspective. Editorially
  professional. Kept for early signal on Middle East events.
- NDTV Profit — Adani-owned since 2023. Editorial independence flagged
  but retained for India business coverage.

**Reviewing this list:** if a feed is reported as misleading or partisan,
the procedure is: (1) remove from the relevant `FEEDS` / `_NEWS_FEEDS`
list, (2) restart `news.runner`, (3) note the removal in this section.

## 7. What's been **rejected** (don't rebuild these)

- **VWAP_REV** — 2025 OOS collapse, max-DD > total P&L. Dropped.
- **Long-options overlay (always-on)** — theta drag exceeded delta capture.
- **Credit Spreads (`spr_swing_ext`)** — on-disk +₹7L is misleading; 2026
  +₹3.5L is contract-roll mechanics, not edge. Strip the rolls and the
  strategy is flat-to-down. Don't redeploy without a theta-first redesign.
- **Trend filter on NIFTY/SENSEX** — tested, cost ~₹70k of edge each.
  BANKNIFTY-only.
- **Lookahead-leaking trend signal** — initial implementation used today's
  close vs N days back. Fixed to use yesterday's close vs (N+1) days back.
  Lesson: every trend / regime feature MUST be tested for lookahead.
- **Filtering broken signals** — saved feedback: don't filter raw signals
  into looking better. Measure raw edge first; layer filters only if they
  improve OOS, not IS.
- **Compounding 90% per trade** — gone. Per-trade risk cap is 2% in the
  compounding engine.

## 8. What's still **pending** (paper-trade gates)

- 1–2 months paper-trading to verify live fill quality matches backtest
  slippage assumptions (BN 30 / NIFTY 10 / SENSEX 20).
- Heartbeat alert for v3 runners ("daemon alive, last bar = X") to detect
  data-feed stalls during market hours.
- Live equity-curve dashboard.
- Cron-schedule for `bots/macro_bot.py --mode daemon` if briefs are wanted
  on MACRO channel beyond the 07:32 newsletter.
- Cron-schedule for `alerts/vp_live_daemon.py` if VP entries are to fire
  alongside v3.

## 9. Where to look first when investigating something

| Question | Look here |
|---|---|
| Why did a live trade fire? | `logs/trade_bot/runner_<inst>.log` |
| Why did a news alert fire? | `logs/news_bot/news_runner.log` |
| What's the latest backtest say? | `reports/weekly_backtest.xlsx` |
| What does this strategy actually do? | `strategies/<name>.py` + Notion strategy page |
| What does the canonical backtest look like? | `python adhoc/run_canonical.py` |
| Where did this old PDF go? | `data_dumps/newsletters_archive/` |
| Was this idea tried before? | This file, section 6 |
| Why was X dropped? | This file, section 7 |
| What's not in cron? | `adhoc/`, `bots/`, `alerts/` (everything except v3 + news + run_daily_report + run_weekly_report) |

## 10. Conventions

- **Cron paths are sacred.** Never move or rename: `run_daily_report.py`,
  `run_weekly_report.py`, `v3/`, `news/`, `data/`, `gen_html_report.py`,
  `gen_report.py`, `daily_report/`, `alerts/telegram.py`, `config.py`,
  `token.env`. They're imported or invoked from cron-scheduled processes.
- **One-shot scripts go in `adhoc/`.** They're never imported by anything in
  the live tree.
- **Big binary outputs go in `data_dumps/`.** PDFs, HTMLs, JSON dumps, ZIPs,
  XLSXs not consumed by code.
- **Logs go in `logs/`.** Subdirected by which bot/process produced them.
- **Don't add new strategies to `run_canonical.py` without re-running OOS.**
  The Notion strategy pages are the source of truth for params.
