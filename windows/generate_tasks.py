"""windows/generate_tasks.py — generate Task Scheduler XML files from a table.

Generates one .xml per task into `windows/scheduled_tasks/`. Re-run on Windows
to regenerate if paths change (edit `BASE_DIR` + `PYTHON` below).

Import all 16 via PowerShell: `windows/IMPORT_TASKS.ps1`
"""
from __future__ import annotations
import pathlib
import datetime as dt

# ── EDIT THESE if your install paths differ on Windows ──────────────────
PYTHON   = r"D:\anaconda3\python.exe"
BASE_DIR = r"D:\Hawala\Hawala v2"

# ── Task table ──────────────────────────────────────────────────────────
# (name, time_HH:MM, daysofweek, kind, args, comment)
# kind: 'daily-mtf' (Mon-Fri), 'weekly-fri', 'weekly-sun'
# args is the python -m / script + args
TASKS = [
    # (name,                    time,   kind,        program, args, comment)
    ('Hawala-Autoheal',         '06:55','daily-mtf', PYTHON,  r'ops\autoheal.py',
        'Self-heal stale caches before market open'),
    ('Hawala-Healthcheck',      '07:25','daily-mtf', PYTHON,  r'ops\healthcheck.py',
        'Daily ops health audit'),
    ('Hawala-DailyReport',      '07:32','daily-mtf', PYTHON,  r'run_daily_report.py',
        'Generate + send Newsletter PDF'),
    ('Hawala-NewsRunner',       '09:00','daily-mtf', PYTHON,  '-m news.runner',
        'News scraper daemon'),
    ('Hawala-Index1mIntraday',  '09:11','daily-mtf', PYTHON,  '-m alerts.index_1m_intraday',
        '1m cache freshness daemon'),
    ('Hawala-V3-NIFTY',         '09:12','daily-mtf', PYTHON,  r'v3\live\runner_nifty.py',
        'NIFTY signal engine'),
    ('Hawala-V3-BANKNIFTY',     '09:12','daily-mtf', PYTHON,  r'v3\live\runner_banknifty.py',
        'BANKNIFTY signal engine'),
    ('Hawala-VPTrail',          '09:12','daily-mtf', PYTHON,  '-m alerts.vp_live_daemon --mode daemon',
        'VP-Trail-Swing live alert daemon'),
    ('Hawala-OptionFlow',       '09:12','daily-mtf', PYTHON,  '-m alerts.option_flow_daemon --mode daemon',
        'Option-flow conviction daemon'),
    ('Hawala-TickRecorder',     '09:12','daily-mtf', PYTHON,  '-m alerts.tick_recorder',
        'Lee-Ready tick recorder for footprint'),
    ('Hawala-Viewer',           '09:13','daily-mtf', PYTHON,  '-m viewer.live_server --host 127.0.0.1 --port 8765',
        'Live footprint viewer backend'),
    ('Hawala-VPPaperExecutor',  '09:12','daily-mtf', PYTHON,  '-m alerts.vp_paper_executor',
        'VP-Trail intraday exit watcher (Telegram on each close)'),
    ('Hawala-DailyFetch',       '16:30','daily-mtf', 'powershell.exe', r'-ExecutionPolicy Bypass -File v3\scripts\daily_fetch.ps1',
        'EOD: fetch 1m + OI + FII + bhavcopy'),
    ('Hawala-VPPaperJournal',   '16:35','daily-mtf', PYTHON,  '-m alerts.vp_paper_journal',
        'VP paper-trade journal + daily summary'),
    ('Hawala-PkillAll',         '03:30','daily-mtf', 'powershell.exe', r'-ExecutionPolicy Bypass -File ops\windows_pkill.ps1',
        'Pre-market cleanup of leftover daemons'),
    ('Hawala-WeeklyReport',     '18:00','weekly-fri', PYTHON,  r'run_weekly_report.py',
        'Friday: weekly performance report'),
    ('Hawala-WeeklyBackfill',   '02:30','weekly-sun', 'powershell.exe', r'-ExecutionPolicy Bypass -File v3\scripts\weekly_backfill.ps1',
        'Sunday: refresh long-history caches'),
]

# ── XML template ────────────────────────────────────────────────────────
TPL = '''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
    <Author>Hawala</Author>
    <URI>\\Hawala\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_boundary}</StartBoundary>
      <Enabled>true</Enabled>
      {schedule_xml}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{program}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''

DOW_MTF = ('<ScheduleByWeek><DaysOfWeek><Monday/><Tuesday/><Wednesday/>'
           '<Thursday/><Friday/></DaysOfWeek><WeeksInterval>1</WeeksInterval>'
           '</ScheduleByWeek>')
DOW_FRI = ('<ScheduleByWeek><DaysOfWeek><Friday/></DaysOfWeek>'
           '<WeeksInterval>1</WeeksInterval></ScheduleByWeek>')
DOW_SUN = ('<ScheduleByWeek><DaysOfWeek><Sunday/></DaysOfWeek>'
           '<WeeksInterval>1</WeeksInterval></ScheduleByWeek>')

KIND_SCHEDULE = {'daily-mtf': DOW_MTF, 'weekly-fri': DOW_FRI, 'weekly-sun': DOW_SUN}


def main() -> None:
    out_dir = pathlib.Path(__file__).resolve().parent / 'scheduled_tasks'
    out_dir.mkdir(exist_ok=True)
    next_monday = dt.date.today()
    while next_monday.weekday() != 0:
        next_monday += dt.timedelta(days=1)

    for name, when, kind, prog, args, desc in TASKS:
        hh, mm = when.split(':')
        sb = f'{next_monday}T{hh}:{mm}:00'
        xml = TPL.format(
            name=name, desc=desc,
            start_boundary=sb,
            schedule_xml=KIND_SCHEDULE[kind],
            program=prog, arguments=args, workdir=BASE_DIR,
        )
        path = out_dir / f'{name}.xml'
        path.write_text(xml, encoding='utf-16')
        print(f'  ✓ {path.name}')

    print(f'\nGenerated {len(TASKS)} task XMLs in {out_dir}')


if __name__ == '__main__':
    main()
