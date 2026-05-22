# Hawala — Deployment guide (post-consolidation)

## What runs where

```
TWO BOTS, TWO PROCESSES:

┌──────────────────────────────────────┐    ┌──────────────────────────────────────┐
│  alert_runner.py                     │    │  bots/macro_bot.py                   │
│                                      │    │                                      │
│  TELEGRAM_BOT_TOKEN  (original bot)  │    │  TELEGRAM_BOT_TOKEN_MACRO  (NEW bot) │
│  TELEGRAM_CHAT_IDS                   │    │  TELEGRAM_CHAT_IDS_MACRO             │
│                                      │    │                                      │
│  CONTENT:                            │    │  CONTENT:                            │
│   • Trade entry alerts               │    │   • 07:30 pre-market brief           │
│   • Trade exit alerts                │    │   • 12:00 mid-day check              │
│   • Per-day P&L summary              │    │   • 16:00 EOD wrap                   │
│                                      │    │   • Ad-hoc regime alerts             │
│  WHEN: 09:00–15:30 IST market days   │    │   WHEN: 07:30 / 12:00 / 16:00 IST    │
└──────────────────────────────────────┘    └──────────────────────────────────────┘
```

## Active strategy stack

The canonical runners now use:

| Strategy | When it fires | Source |
|---|---|---|
| **Futures ORB** | Gap 50–100 pts, Tue/Wed/Fri | `strategies/orb.py` |
| **Options ORB** | Gap > 100 pts, Tue/Wed/Fri | `strategies/options_orb.py` |
| **VP-Trail-Swing** | Pierce of 70% Value Area + reversal candle | `strategies/vp_trailing_swing.py` |

Per-instrument tuning lives in `run_canonical.py`'s `CANONICAL_PARAMS`.

### Dropped (archived in `_archived/`)

- VWAP_REV — decayed OOS 2025 (-₹2,195 avg/trade)
- Long-options overlay — theta drag exceeds delta capture
- Credit spreads — net -₹4.8L on 4½ years
- Original simple-target volume profile — superseded by VP-Trail-Swing

## token.env layout

```sh
# Groww auth
GROWW_API_KEY=...
GROWW_TOTP_SECRET=...

# Original bot — TRADE alerts (entries, exits, P&L)
TELEGRAM_BOT_TOKEN=<original bot token>
TELEGRAM_CHAT_IDS=<comma-separated chat IDs>

# NEW macro/news bot — pre-market, mid-day, EOD briefs ONLY
TELEGRAM_BOT_TOKEN_MACRO=<new bot token>
TELEGRAM_CHAT_IDS_MACRO=<comma-separated chat IDs>
```

## Smoke tests

```sh
# 1. Verify macro bot wiring (sends one test message to the macro channel)
python -m bots.macro_bot --mode test

# 2. Pre-market brief (one-off)
python -m bots.macro_bot --mode premarket

# 3. Trade-alert runner (continuous, original bot)
python alert_runner.py
```

## Production schedule (cron / launchd) — CURRENT

```sh
# Daily newsletter PDF — MACRO bot only, no summary message
32  7  * * 1-5  cd /path/to/hawala && python3 run_daily_report.py \
                  >> logs/macro_bot/daily_report-$(date +\%Y\%m\%d).log 2>&1

# News runner — TRADE bot, ad-hoc event alerts
0   9  * * 1-5  cd /path/to/hawala && nohup caffeinate -i python3 -m news.runner \
                  > /dev/null 2>&1 &

# v3 live runners — TRADE bot, intraday OI/FII signal entries
12  9  * * 1-5  cd /path/to/hawala && nohup caffeinate -i python3 v3/live/runner_nifty.py \
                  > /dev/null 2>&1 &
12  9  * * 1-5  cd /path/to/hawala && nohup caffeinate -i python3 v3/live/runner_banknifty.py \
                  > /dev/null 2>&1 &

# Defensive kill (also catches archived/v2_legacy/alert_runner.py if run manually)
30  3  * * 1-5  pkill -f "runner_nifty.py"; pkill -f "runner_banknifty.py"; \
                pkill -f "alert_runner.py"; pkill -f "news\.runner"

# EOD candle / option-OI / bhavcopy fetch
30 16  * * 1-5  cd /path/to/hawala && bash v3/scripts/daily_fetch.sh \
                  >> logs/reports/daily_fetch.log 2>&1

# Weekly cache backfill
30  2  * * 0    cd /path/to/hawala && bash v3/scripts/weekly_backfill.sh \
                  >> logs/reports/weekly_backfill.log 2>&1

# Friday weekly trade summary — TRADE bot
0  18  * * 5    cd /path/to/hawala && python3 run_weekly_report.py \
                  >> logs/reports/weekly_report-$(date +\%Y\%m\%d).log 2>&1
```

`alert_runner.py` is **NOT** scheduled — replaced by the v3 runners. It now
lives in `archived/v2_legacy/`. The defensive `pkill` line still names it.

VP-Trail signal daemon (LIVE since 2026-05-12, alongside v3):

```sh
# Same 09:12 start as v3 runners; daemon exits at 15:30 on its own
12  9  * * 1-5  cd /path/to/hawala && nohup caffeinate -i python3 -m alerts.vp_live_daemon --mode daemon > /dev/null 2>&1 &
```

Also add to the defensive pkill line:

```sh
30  3  * * 1-5  pkill -f "runner_nifty.py"; pkill -f "runner_banknifty.py"; \
                pkill -f "alert_runner.py"; pkill -f "news\.runner"; \
                pkill -f "alerts.vp_live_daemon"
```

Optional add-ons (NOT currently scheduled):

```sh
# Mid-day + EOD MACRO briefs (in addition to the 07:32 newsletter)
0  12  * * 1-5  cd /path/to/hawala && python3 -m bots.macro_bot --mode midday
0  16  * * 1-5  cd /path/to/hawala && python3 -m bots.macro_bot --mode eod
```

## Backtest reproduction

```sh
# Final canonical config (1-lot, no compounding)
python run_canonical.py

# Full ORB + OPT_ORB + VP-Trail-Swing pipeline with compounding
python run_baseline.py
```

## Weekly artefacts (refreshed every weekend)

Two reports are auto-rebuilt by `reports/refresh_weekend.sh` after the
Friday close:

| File                          | Purpose                                          |
|-------------------------------|--------------------------------------------------|
| `reports/weekly_backtest.xlsx` | One-row-per-strategy summary + per-strategy monthly tabs + per-strategy trade tabs (5 strategies × 2 tabs + summary = 11 sheets) |
| `research/trade_explorer.html` | Interactive viewer for **all** backtest trades with strategy chip-toggles (turn each strategy on/off) |

```sh
# Manual refresh
bash reports/refresh_weekend.sh

# cron — every Saturday 06:00 IST
0 6 * * 6  cd /path/to/hawala && bash reports/refresh_weekend.sh
```

Logs land in `reports/logs/refresh-YYYYMMDD-HHMM.log`.

## Critical pre-live items

Before deploying real capital:

1. **Per-trade risk cap** — modify `backtest/compounding_engine.py` to cap loss per trade at 2% of equity. Currently it deploys 90% blindly.
2. **Daily loss limit** — halt all new entries if intraday equity is down 5%.
3. **Slippage realism** — current backtest assumes 5–10 pt slippage. Real fills on stop orders during fast markets can be 50–200 pts worse.
4. **1-2 months paper trading** — verify live signal generation matches backtest before any real money.
5. **Live equity-curve dashboard** — to detect strategy decay early.

See [VERDICT in this thread] for the full risk picture.

## Files reference

| File | Purpose |
|---|---|
| `run_canonical.py` | 1-lot reproducible backtest of the final config |
| `run_baseline.py` | Compounded backtest of the full stack (ORB + OPT_ORB + VP-Trail-Swing) |
| `alert_runner.py` | Live trade-alert daemon (original bot) |
| `bots/macro_bot.py` | Macro/news daemon (NEW bot) |
| `strategies/vp_trailing_swing.py` | Canonical VP strategy with all knobs |
| `strategies/orb.py` | v2 ORB |
| `strategies/options_orb.py` | v2 Options ORB |
| `data/fetch_15m_futures.py` | 15m futures cache fetcher (idempotent) |
| `research/trade_explorer.py` | Per-trade chart visualisation |
| `_archived/` | Dropped strategies (kept for reference, not imported) |
