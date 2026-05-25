# Hawala — Roadmap & Open Action Items

*Last updated: 2026-05-25 — Windows migration REVERTED. Mac is production.*

This is the working punch-list. Items roll off as they ship.

---

## ⚠️ Windows migration reverted (May 25 2026)

After two days running on Windows, Anaconda on `D:\anaconda3` corrupted
overnight (every .exe became a 0-byte stub). Couldn't be salvaged in time;
production switched back to Mac mid-day Mon 25 May. The `windows/` subtree
+ `docs/WINDOWS_MIGRATION.md` stay in repo as reference, not active.

**If we ever retry**: clean Anaconda uninstall + Defender exclusion BEFORE
install + use D: but with `D:\anaconda` not `D:\anaconda3` (avoid the
Microsoft Store python.exe filename alias conflict).

---

## ✅ Shipped this month

1. **Footprint pipeline** — tick_recorder, index_1m_intraday, footprint.py builder, live viewer (FastAPI + Plotly.js), live DOM pane, session vol profile
2. **VP-Trail paper journal** (`alerts/vp_paper_journal.py`) — daily 16:35 cron, journal CSV + TRADE-bot Telegram daily P&L
3. **VP-Trail intraday executor** (`alerts/vp_paper_executor.py`) — polls every 60s during market, Telegrams each close in real time
4. **Re-entry cooldown** (`v3/live/reentry_cooldown.py`) — blocks v3 same-direction same-strike re-entry within 90 min of FLOW_SL
5. **Footprint research framework** (`research/footprint_features.py`, `research/footprint_correlation.py`) — joins trades + footprint context for eventual veto-layer regression
6. **Self-healing routines** (`ops/autoheal.py`, `ops/healthcheck.py`)
7. **News pipeline tightening** — anchor-word gate, hour/day/theme caps, EOD digest
8. **Mac → Windows migration** — Task Scheduler XMLs, PowerShell ports, full system handover
9. **Mac → Windows SSH heartbeat** — every 10 min via launchd, MACRO bot alerts on outage
10. **Windows auto git-pull** (`ops/git_sync.ps1`) — daily 06:50 task, --ff-only, safe abort on dirty tree

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

---

## 🔵 Open — medium priority

### Fix v3 runner gate-alert Telegram parse errors
Both `runner_nifty.py` and `runner_banknifty.py` emit gate-block alerts
(VOL_GATE for NIFTY, REGIME_GATE for BANKNIFTY) that Telegram rejects
with HTTP 400 "Unsupported start tag at byte 92/101". Probably the alert
message uses `|` separators or em-dashes that, when sent with
`parse_mode=HTML`, get interpreted as unclosed tags. Fix: either escape
< / > / & in those alert strings, or switch the gate alerts to plain
text (parse_mode=None). Non-blocking — alerts still get attempted, just
fail at Telegram. Cosmetic but loud in logs.

Forensic: 2026-05-25 NIFTY vol-gate alert (`logs/trade_bot/runner_nifty.log`)
and BANKNIFTY regime-gate alert both produced this same 400.

### Retry Windows migration (parked, requires clean prep)
The 23-25 May attempt failed because legacy C:\anaconda3 artifacts
collided with the new D:\anaconda3 install. Retry only if these steps
are followed in order:
1. Disable Microsoft Store python.exe / python3.exe App Execution Aliases
   via Settings BEFORE installing Anaconda
2. Wipe HKLM:\SOFTWARE\Python, HKCU:\SOFTWARE\Python, HKLM:\SOFTWARE\Anaconda*
3. Strip all *anaconda* and *python* entries from PATH (Machine + User)
4. Add Defender exclusion `D:\anaconda` BEFORE install
5. Install to `D:\anaconda` (no `3`) to avoid Store-alias filename match
6. UNCHECK "Register Anaconda as default Python" during install
7. Verify python.exe is a real PE: `[IO.File]::ReadAllBytes(...).Length > 100000`

### Log redirection for 7 one-shot tasks
Most daemons have their own loggers writing to `logs/trade_bot/*.log`. These 7 don't and rely on Task Scheduler History:
- Hawala-DailyReport, Autoheal, Healthcheck, DailyFetch, VPPaperJournal, WeeklyReport, WeeklyBackfill

Fix: wrap each task command in `cmd.exe /c "python script.py >> logs\reports\<name>-%date%.log 2>&1"`. Edit generate_tasks.py, regenerate XMLs, re-import.

### Off-LAN access for viewer
Mac is production now. To view from a phone or another laptop:
- Change `viewer.live_server` cron to bind `0.0.0.0` instead of `127.0.0.1`
- OR install Tailscale on phone + Mac for off-LAN access
- Either way: bookmark `http://<mac-ip>:8765/` on the secondary device

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

### Mac sleep prevention (waking for crons)
The 19-May incident: Mac slept overnight, missed 06:55–09:13 crons. The
quick fix is `sudo pmset -a sleep 0` + lid open + plugged in. Long-term
fix is `sudo pmset repeat wakeorpoweron MTWRF 06:30:00` so Mac
auto-wakes for crons even if lid closed. Document in CONTEXT.md.

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
