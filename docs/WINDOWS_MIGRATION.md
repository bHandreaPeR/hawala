# Hawala v2 — Windows Migration Plan

*Target day: Saturday, May 23 2026 (non-market). Goal: by Monday 09:12 IST, the Windows box is running every cron the Mac currently runs.*

---

## At-a-glance

| Layer | Mac (today) | Windows (after) |
|---|---|---|
| Scheduler | `cron` (crontab -e) | **Task Scheduler** (`schtasks` / `taskschd.msc`) |
| Wake-on-schedule | `pmset repeat …` (unreliable) | Task Scheduler "Wake the computer to run this task" (reliable) |
| Sleep prevention per-process | `caffeinate -i …` | Drop entirely — TS wake handles it |
| Process kill | `pkill -f …` | `taskkill /F /IM …` or `Get-Process … | Stop-Process` |
| Shell scripts | bash (`*.sh`) | PowerShell (`*.ps1`) |
| Python | `/opt/anaconda3/bin/python3` | `C:\anaconda3\python.exe` (or wherever) |
| Path style | `/Users/…/Hawala v2` | `C:\Hawala\Hawala v2` (no spaces preferred) |

**Total work**: ~4 hours focused.

---

## Phase 0 — Pre-flight on Mac (15 min, do TODAY before bed)

1. **Verify the repo is clean and pushed.** (See "GitHub push" section below.)
2. **Bundle the things git can't carry** — secrets + caches. Create a tarball you'll move via USB / iCloud / scp:
   ```bash
   cd "/Users/subhransubaboo/Claude Projects/Hawala v2/Hawala v2"
   tar czvf ~/hawala_secrets_and_caches.tar.gz \
       token.env \
       v3/cache/*.pkl \
       v3/cache/pcr_daily.csv \
       v3/cache/option_flow_*.json \
       news/state/ \
       alerts/.*.json \
       trade_logs/*.csv
   ls -lh ~/hawala_secrets_and_caches.tar.gz
   ```
   Expected size: ~400-500 MB (mostly the 1m + option-OI pickles).
3. **Copy the tarball to the Windows box** — easiest is iCloud Drive or a USB stick. Verify size on Windows side matches.
4. **Pause Mac crons for the day** so they don't fight Windows during cutover:
   ```bash
   crontab -l > ~/hawala_crontab_backup.txt
   crontab -r       # removes all crons (you have the backup!)
   ```

---

## Phase 1 — Windows machine prep (~45 min)

### 1.1 Install fundamentals
- **Anaconda Python 3.13** — installer from anaconda.com. Install path `C:\anaconda3\` (avoid spaces, avoid Program Files). Check "Add to PATH".
- **Git for Windows** — gitforwindows.org. Gives you `git`, `bash`, `ssh`. Default options are fine.
- **Google Chrome** — for the viewer + headless PDF generation.
- **Notepad++** or **VS Code** — for editing `.ps1` and config files.

### 1.2 Verify
Open **PowerShell as Administrator** and run:
```powershell
python --version          # should show 3.13.x
git --version             # should show 2.x
"C:\anaconda3\python.exe" --version
```

### 1.3 Clone the repo
```powershell
mkdir C:\Hawala
cd C:\Hawala
git clone https://github.com/bHandreaPeR/hawala-v2 "Hawala v2"
cd "Hawala v2"
```

### 1.4 Install Python packages
```powershell
C:\anaconda3\python.exe -m pip install -r requirements.txt
```
Take coffee. ~5 minutes.

### 1.5 Restore secrets + caches
- Extract the tarball into the repo root:
  ```powershell
  cd "C:\Hawala\Hawala v2"
  tar -xzvf C:\path\to\hawala_secrets_and_caches.tar.gz
  ```
- Verify: `Get-ChildItem token.env, v3\cache\candles_1m_NIFTY.pkl` — both must exist.

### 1.6 Sanity-test before touching scheduler
```powershell
# Auth test — should print "Auth validated OK"
C:\anaconda3\python.exe -c "from v3.data.fetch_1m_NIFTY import _get_groww, _validate_auth; _validate_auth(_get_groww())"

# Strategy import test
C:\anaconda3\python.exe -c "from strategies.vp_trailing_swing import run_vp_trailing_swing; print('OK')"

# Viewer port test (background, then kill)
Start-Process -NoNewWindow C:\anaconda3\python.exe -ArgumentList "-m viewer.live_server"
Start-Sleep 4
curl http://127.0.0.1:8765/config
Stop-Process -Name python -Force
```

If any of these fail → fix BEFORE touching cron. Most likely cause is missing pip pkg or wrong `token.env` location.

---

## Phase 2 — Task Scheduler setup (~90 min)

Open **Task Scheduler** (`taskschd.msc`) → "Create Task" (not "Create Basic Task" — we need the wake-on-schedule option).

### 2.1 The shared task template

For EVERY task, use these settings:

| Tab | Setting |
|---|---|
| **General** | Run only when user logged on  →  **Run whether user is logged on or not** (cleaner) |
| General | ☑ Run with highest privileges |
| **Triggers** | New → Daily → Start: 2026-05-25 06:30 → Recur every 1 day → Days of week: Mon-Fri only |
| **Conditions** | ☑ **Wake the computer to run this task** ← critical |
| Conditions | ☑ Start the task only if the computer is on AC power (optional — depends on whether laptop) |
| **Settings** | ☑ If the task fails, restart every 1 minute, up to 3 times |
| Settings | ☑ Stop the task if it runs longer than 8 hours (kills hung daemons) |

### 2.2 The 14 tasks (in order, with action details)

For each, **Action → Start a program**:

| # | Name | Time IST | Program | Arguments | Start in |
|---|---|---|---|---|---|
| 1 | Hawala-DailyReport | 07:32 | `C:\anaconda3\python.exe` | `run_daily_report.py` | `C:\Hawala\Hawala v2` |
| 2 | Hawala-Autoheal | 06:55 | `C:\anaconda3\python.exe` | `ops\autoheal.py` | `C:\Hawala\Hawala v2` |
| 3 | Hawala-Healthcheck | 07:25 | `C:\anaconda3\python.exe` | `ops\healthcheck.py` | `C:\Hawala\Hawala v2` |
| 4 | Hawala-NewsRunner | 09:00 | `C:\anaconda3\python.exe` | `-m news.runner` | `C:\Hawala\Hawala v2` |
| 5 | Hawala-Index1mIntraday | 09:11 | `C:\anaconda3\python.exe` | `-m alerts.index_1m_intraday` | `C:\Hawala\Hawala v2` |
| 6 | Hawala-V3-NIFTY | 09:12 | `C:\anaconda3\python.exe` | `v3\live\runner_nifty.py` | `C:\Hawala\Hawala v2` |
| 7 | Hawala-V3-BANKNIFTY | 09:12 | `C:\anaconda3\python.exe` | `v3\live\runner_banknifty.py` | `C:\Hawala\Hawala v2` |
| 8 | Hawala-VPTrail | 09:12 | `C:\anaconda3\python.exe` | `-m alerts.vp_live_daemon --mode daemon` | `C:\Hawala\Hawala v2` |
| 9 | Hawala-OptionFlow | 09:12 | `C:\anaconda3\python.exe` | `-m alerts.option_flow_daemon --mode daemon` | `C:\Hawala\Hawala v2` |
| 10 | Hawala-TickRecorder | 09:12 | `C:\anaconda3\python.exe` | `-m alerts.tick_recorder` | `C:\Hawala\Hawala v2` |
| 11 | Hawala-Viewer | 09:13 | `C:\anaconda3\python.exe` | `-m viewer.live_server --host 127.0.0.1 --port 8765` | `C:\Hawala\Hawala v2` |
| 12 | Hawala-DailyFetch | 16:30 | `C:\anaconda3\python.exe` | `v3\scripts\daily_fetch.py` *(needs porting from .sh — see Phase 3)* | `C:\Hawala\Hawala v2` |
| 13 | Hawala-VPPaperJournal | 16:35 | `C:\anaconda3\python.exe` | `-m alerts.vp_paper_journal` | `C:\Hawala\Hawala v2` |
| 14 | Hawala-PkillAll | 03:30 | `powershell.exe` | `-File ops\windows_pkill.ps1` *(see Phase 3)* | `C:\Hawala\Hawala v2` |
| 15 | Hawala-WeeklyReport | Fri 18:00 | `C:\anaconda3\python.exe` | `run_weekly_report.py` | `C:\Hawala\Hawala v2` |
| 16 | Hawala-WeeklyBackfill | Sun 02:30 | `C:\anaconda3\python.exe` | `v3\scripts\weekly_backfill.py` *(needs porting)* | `C:\Hawala\Hawala v2` |

**Tip**: use `schtasks /create /XML <file>.xml /TN <name>` to import all 16 at once instead of clicking through the GUI. I'll generate the 16 XML files as a follow-up if you want.

### 2.3 IST → Local TZ conversion

If your Windows box runs in IST already, the times above are literal. If it's UTC or another zone, convert: IST = UTC + 5:30.

Check current TZ:
```powershell
Get-TimeZone
```
If not "India Standard Time", set:
```powershell
Set-TimeZone -Id "India Standard Time"
```

---

## Phase 3 — Bash → PowerShell ports (~30 min)

Three small files need translation:

### 3.1 `v3/scripts/daily_fetch.sh` → `v3/scripts/daily_fetch.ps1`
Calls the 4 fetchers in sequence. Pattern:
```powershell
# v3/scripts/daily_fetch.ps1
$ErrorActionPreference = "Continue"
Set-Location "C:\Hawala\Hawala v2"
$py = "C:\anaconda3\python.exe"
& $py v3\data\fetch_1m_NIFTY.py
& $py v3\data\fetch_1m_BANKNIFTY.py
& $py v3\data\fetch_1m_SENSEX.py
& $py v3\data\fetch_option_oi_NIFTY.py
& $py v3\data\fetch_option_oi_BANKNIFTY.py
& $py v3\data\fetch_fii_cash.py
& $py v3\data\fetch_fii_fo.py
& $py v3\data\fetch_bhavcopy_nifty.py
& $py v3\data\fetch_bhavcopy_banknifty.py
& $py v3\data\build_pcr_daily.py
```

### 3.2 `viewer/launch.sh` → `viewer/launch.ps1`
```powershell
# viewer/launch.ps1
$url = "http://127.0.0.1:8765/"
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$profile = "$env:USERPROFILE\.hawala_viewer_profile"
New-Item -ItemType Directory -Force -Path $profile | Out-Null
& $chrome --app=$url --user-data-dir=$profile --window-size=1400,900 --no-first-run
```

### 3.3 NEW: `ops/windows_pkill.ps1` — replaces the 03:30 `pkill -f …` line
```powershell
# ops/windows_pkill.ps1 — kill all Hawala daemons each morning
$patterns = @(
    "runner_nifty", "runner_banknifty",
    "news.runner",
    "alerts.vp_live_daemon", "alerts.option_flow_daemon",
    "alerts.tick_recorder", "alerts.index_1m_intraday",
    "alerts.vp_paper_journal",
    "viewer.live_server"
)
foreach ($p in $patterns) {
    Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like "*$p*"} |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
```

I'll create these three files in a follow-up commit.

---

## Phase 4 — First-run verification (~30 min, Saturday afternoon)

After all tasks are imported:

### 4.1 Manual smoke test (don't wait for cron)
Open PowerShell at `C:\Hawala\Hawala v2`:
```powershell
# 1. Healthcheck — should show all PASS except today's logs (those need crons to have fired)
C:\anaconda3\python.exe ops\healthcheck.py --dry

# 2. VP paper journal — should add zero new trades but build a summary
C:\anaconda3\python.exe -m alerts.vp_paper_journal

# 3. Viewer — should start and respond
Start-Process C:\anaconda3\python.exe -ArgumentList "-m viewer.live_server"
Start-Sleep 5
curl http://127.0.0.1:8765/config
```

### 4.2 Force-trigger one cron via Task Scheduler
Right-click any task → **Run** — verify it executes successfully. Check its history tab.

### 4.3 Verify wake-on-schedule
1. Set a temporary task: 5 minutes from now, "Wake the computer", action = nothing useful (e.g. `C:\Windows\System32\cmd.exe /c echo ok > C:\Hawala\wake_test.txt`).
2. Lock the screen, close the lid (laptop) or let it idle to sleep.
3. Come back after the trigger time. The file should exist + Task Scheduler history should show success.

If wake fails: check BIOS settings (some laptops need "Wake on RTC" / "Wake on LAN" enabled). Most desktops work out-of-the-box.

---

## Phase 5 — Monday cutover (final hour)

Sunday night before bed:

1. Mac: keep crontab empty (it's already removed from Phase 0).
2. Windows: confirm Task Scheduler shows all 16 tasks "Ready" status.
3. Windows: ensure laptop plugged in, lid open (or set to "do nothing when lid closed"), on Wi-Fi.
4. Windows: `pmset` equivalent — go to **Settings → System → Power & battery → Screen and sleep** → set "When plugged in, PC goes to sleep after" to "Never" (or trust Task Scheduler wake — both work).

Monday 06:30:
- Windows wakes via Task Scheduler at 06:30 trigger.
- 06:55: autoheal runs.
- 07:25: healthcheck runs.
- 07:32: daily_report fires → Newsletter PDF on Telegram MACRO bot.
- 09:00–09:13: all daemons launch.
- Monitor Telegram bots for the first 30 minutes.
- Open the viewer manually: `viewer\launch.ps1`.

If anything's wrong → restore Mac crontab (`crontab ~/hawala_crontab_backup.txt`) as the emergency fallback.

---

## What I'll generate as the follow-up commit

| File | Purpose |
|---|---|
| `requirements.txt` | done — already in this commit |
| `windows/scheduled_tasks/*.xml` × 16 | Importable Task Scheduler definitions (`schtasks /create /XML`) |
| `v3/scripts/daily_fetch.ps1` | PowerShell port |
| `v3/scripts/weekly_backfill.ps1` | PowerShell port |
| `viewer/launch.ps1` | PowerShell port |
| `ops/windows_pkill.ps1` | PowerShell replacement for `pkill -f …` chain |

I'll do those tomorrow morning before you start the migration. They're mechanical — no design decisions left.

---

## Rollback plan (in case of disaster)

If anything's catastrophically wrong on Monday morning:

1. Restore Mac crontab: `crontab ~/hawala_crontab_backup.txt`
2. Wake Mac manually (`sudo pmset -a sleep 0`).
3. Run autoheal once: `cd "/Users/subhransubaboo/Claude Projects/Hawala v2/Hawala v2" && /opt/anaconda3/bin/python3 ops/autoheal.py`
4. Reply to me with: which task failed, the Task Scheduler error code, the python log tail.

Mac → Windows is a one-way arrow only if you delete the Mac caches. Keep them for at least 2 weeks as the cold backup.

---

## Cost / risk summary

| Risk | Likelihood | Mitigation |
|---|---|---|
| `token.env` missing on Windows | High if forgotten | Tarball Phase 0 covers it; smoke test in Phase 1.6 catches it |
| Path with spaces breaks something | Medium | Quote every path in PowerShell + use `C:\Hawala\Hawala v2` not Program Files |
| Wake-on-schedule disabled in BIOS | Low | Phase 4.3 verification |
| Groww auth fails first run | Low | The retry logic handles it; check token.env on disk |
| Task Scheduler runs program as wrong user | Medium | Use "Run whether user is logged on or not" + tick "Run with highest privileges" |
| Some Python package fails to install on Windows | Low — all listed pkgs are pure-Python or have Windows wheels | `pip install` errors are loud; install one-by-one if needed |
| Mac caches go out of sync after cutover | High if Mac left on | After cutover, archive Mac repo to `~/hawala-archive-2026-05-23` and stop the crons |

---

## Time budget

| Phase | Time | Window |
|---|---|---|
| Phase 0 — Mac prep | 15 min | Friday night or Saturday morning |
| Phase 1 — Windows install | 45 min | Saturday morning |
| Phase 2 — Task Scheduler | 90 min (or 20 min if I generate XMLs) | Saturday afternoon |
| Phase 3 — PowerShell ports | 30 min (I generate them) | Saturday afternoon |
| Phase 4 — Verification | 30 min | Saturday afternoon |
| Phase 5 — Monday cutover | 30 min monitoring | Monday 09:00-09:30 |
| **Total** | **~4 hours focused, spread over Sat-Mon** | |
