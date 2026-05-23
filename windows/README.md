# Hawala v2 — Windows Setup (1-page quickstart)

Full plan is in `docs/WINDOWS_MIGRATION.md`. This is the **happy path**.

## Pre-reqs (install once)

1. **Anaconda Python 3.13** → install to `D:\anaconda3\` (avoid spaces, avoid Program Files)
2. **Git for Windows** → default options
3. **Google Chrome**
4. Set TZ to **India Standard Time** if not already: `Set-TimeZone -Id "India Standard Time"`

## 4-step install

Open **PowerShell as Administrator**:

```powershell
# 1. Clone
mkdir D:\Hawala
cd D:\Hawala
git clone https://github.com/bHandreaPeR/hawala.git "Hawala v2"
cd "Hawala v2"

# 2. Install Python deps
D:\anaconda3\python.exe -m pip install -r requirements.txt

# 3. Restore secrets + caches (point to your tarball path)
tar -xzvf "C:\path\to\hawala_secrets_and_caches.tar.gz"

# 4. Import all 16 scheduled tasks
.\windows\IMPORT_TASKS.ps1
```

That's it. The 16 tasks are now in Task Scheduler under `\Hawala\`.

## Verify before Monday

```powershell
# Should print "Auth validated OK"
D:\anaconda3\python.exe -c "from v3.data.fetch_1m_NIFTY import _get_groww, _validate_auth; _validate_auth(_get_groww())"

# Should pass except for "today's log" entries that need crons to have fired
D:\anaconda3\python.exe ops\healthcheck.py --dry

# Manually trigger one task from PowerShell to test
schtasks /Run /TN "\Hawala\Hawala-Healthcheck"
# (Check its history tab in taskschd.msc)
```

## Sleep / wake

Task Scheduler will **wake the computer** at every trigger (built into each XML via `<WakeToRun>true</WakeToRun>`). No `pmset`-style ritual needed.

Optional: prevent any idle sleep entirely
```powershell
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
```

## File map

```
windows/
├── README.md                       # this file
├── generate_tasks.py               # regenerate XMLs if paths change
├── IMPORT_TASKS.ps1                # one-shot import (run as admin)
└── scheduled_tasks/                # 16 .xml files (one per task)
    ├── Hawala-Autoheal.xml         # 06:55 weekdays
    ├── Hawala-Healthcheck.xml      # 07:25 weekdays
    ├── Hawala-DailyReport.xml      # 07:32 weekdays — Newsletter PDF
    ├── Hawala-NewsRunner.xml       # 09:00 weekdays
    ├── Hawala-Index1mIntraday.xml  # 09:11 weekdays
    ├── Hawala-V3-NIFTY.xml         # 09:12 weekdays
    ├── Hawala-V3-BANKNIFTY.xml     # 09:12 weekdays
    ├── Hawala-VPTrail.xml          # 09:12 weekdays
    ├── Hawala-OptionFlow.xml       # 09:12 weekdays
    ├── Hawala-TickRecorder.xml     # 09:12 weekdays
    ├── Hawala-Viewer.xml           # 09:13 weekdays
    ├── Hawala-DailyFetch.xml       # 16:30 weekdays
    ├── Hawala-VPPaperJournal.xml   # 16:35 weekdays
    ├── Hawala-PkillAll.xml         # 03:30 weekdays
    ├── Hawala-WeeklyReport.xml     # Fri 18:00
    └── Hawala-WeeklyBackfill.xml   # Sun 02:30

v3/scripts/
├── daily_fetch.ps1                 # Windows port of daily_fetch.sh
└── weekly_backfill.ps1             # Windows port of weekly_backfill.sh

viewer/
└── launch.ps1                      # Open viewer in Chrome --app mode

ops/
└── windows_pkill.ps1               # Replaces the 03:30 pkill chain
```

## If you change install paths

Edit `windows/generate_tasks.py`:
```python
PYTHON   = r"D:\anaconda3\python.exe"
BASE_DIR = r"D:\Hawala\Hawala v2"
```
Then re-run `python windows/generate_tasks.py && powershell .\windows\IMPORT_TASKS.ps1`.

## Rollback

If anything breaks Monday and you need to revert to Mac:
```powershell
# Disable all Hawala tasks
schtasks /Change /TN "\Hawala\*" /DISABLE
```
Then on Mac:
```bash
crontab ~/hawala_crontab_backup.txt
```
