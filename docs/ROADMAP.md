# Hawala — Roadmap & Open Action Items

*Last updated: 2026-05-23 (post-migration + heartbeat)*

This is the working punch-list. Items roll off as they ship.

---

## ✅ Shipped this week (May 18–23)

1. **Footprint pipeline** — tick_recorder, index_1m_intraday, footprint.py builder, live viewer (FastAPI + Plotly.js), live DOM pane, session vol profile
2. **VP-Trail paper journal** (`alerts/vp_paper_journal.py`) — daily 16:35 cron, journal CSV + TRADE-bot Telegram daily P&L
3. **VP-Trail intraday executor** (`alerts/vp_paper_executor.py`) — polls every 60s during market, Telegrams each close in real time
4. **Re-entry cooldown** (`v3/live/reentry_cooldown.py`) — blocks v3 same-direction same-strike re-entry within 90 min of FLOW_SL
5. **Footprint research framework** (`research/footprint_features.py`, `research/footprint_correlation.py`) — joins trades + footprint context for eventual veto-layer regression
6. **Self-healing routines** (`ops/autoheal.py`, `ops/healthcheck.py`)
7. **News pipeline tightening** — anchor-word gate, hour/day/theme caps, EOD digest
8. **Mac → Windows migration** — Task Scheduler XMLs, PowerShell ports, full system handover
9. **Mac → Windows SSH heartbeat** — every 10 min via launchd, MACRO bot alerts on outage

---

## 🟡 Open — high priority

### Wait for data (3 weeks)
- **Footprint correlation analysis** — once `trade_logs/footprint_features.csv` has 30+ rows with `fp_data_available=True`, regress `win ~ fp_*`. If signal emerges, build the veto layer.
  - Status: 26 trades extracted today, only 2 with tick context. ~10-15 trades/week expected.
  - Trigger: rerun `python research/footprint_correlation.py` weekly.

### Telegram bot enhancements
- **`/log <name>` command** — on-demand log fetch via Telegram. Bot reads the file on Windows, sends back last 50 lines.
  - Why: debug from anywhere without SSH.
  - ~40 LOC bot handler + integrate into news.runner or a new tiny daemon.

- **`/status` command** — show all daemons alive + last activity per Telegram.

### Auto git-pull on Windows
- **Daily 06:50 task** that runs `git pull --ff-only` before autoheal/healthcheck/runners. So you push from Mac → next morning Windows runs it.
  - File: `ops/git_sync.ps1`
  - XML: `windows/scheduled_tasks/Hawala-GitSync.xml`
  - ~30 LOC. Idempotent. Safe (--ff-only never rewrites).

---

## 🔵 Open — medium priority

### Log redirection for 7 one-shot tasks
Most daemons have their own loggers writing to `logs/trade_bot/*.log`. These 7 don't and rely on Task Scheduler History:
- Hawala-DailyReport, Autoheal, Healthcheck, DailyFetch, VPPaperJournal, WeeklyReport, WeeklyBackfill

Fix: wrap each task command in `cmd.exe /c "python script.py >> logs\reports\<name>-%date%.log 2>&1"`. Edit generate_tasks.py, regenerate XMLs, re-import.

### Off-LAN access for viewer + SSH
Currently both require Mac and Windows on same Wi-Fi.
- **Tailscale** install on both machines. 5-min one-time setup. Free for ≤3 devices.
- Once installed: SSH and viewer URL work from anywhere.

### Telegram health summary on TRADE bot
Every Friday EOD, send a Telegram with the week's tally:
- v3 trades, VP-Trail trades, paper P&L for both
- Healthcheck pass rate
- Daemon uptime
- Top 3 missed signals (if cooldown blocked good entries)

---

## 🟢 Open — low priority / nice-to-have

### Branch hygiene
- `main` is currently the only branch. Going forward: develop on feat branches, merge to main, delete feat branch immediately. No more multi-branch sprawl.

### F6 veto evaluation (option_flow)
- After 2-3 weeks of v3 trades with current option_flow conviction logging, evaluate whether to wire the F6 veto signal into entries.

### Option_flow signal-quality eval
- After 1 week of option_flow_trace_*.ndjson data, compute z-window false-positive rate.

### Backup tarball weekly to OneDrive
- Windows Task Scheduler runs Sundays at 17:00 to dump `token.env` + `v3/cache/*.pkl` + journals into a OneDrive folder.
- ~20 LOC PowerShell.

### Migration to a proper hosted VPS (someday)
Replace the Windows-at-home setup with a 1-vcpu cloud box (Vultr/Hetzner/DO ~₹500/mo). Pros: never sleeps, fixed IP, easier monitoring. Cons: cost, slight latency to NSE.

---

## 🔴 Honest non-goals

These have been explored and parked. Don't revisit without new data.

- **Mother-Baby candle strategy** — tested v1, v2 strict, and 340-day spot. No edge.
- **Z-window option-flow lead/lag POC** — marginal predictive power, not worth wiring as a primary signal.
- **Candlestick pattern strategy** — AUC 0.562, basically random.
- **Order-book heatmap (full Bookmap analog)** — needs TBT/MBO feed (₹50L-2Cr/year colo setup), economic dead-end at retail.
- **VP-Trail signal-time spreading across instruments** — backtest showed correlation negligible, not worth complexity.

---

## Tracking discipline

When work begins on an item:
1. Move it from this file to a feature branch
2. When merged to main, delete from this file
3. Add to "Shipped" section with one-line summary
4. Update `MEMORY.md` index if it's a major capability
