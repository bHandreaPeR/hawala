# ops/windows_pkill.ps1 — Pre-market cleanup of any leftover Hawala daemons.
# Equivalent of the Mac cron line:
#   30 3 * * 1-5 pkill -f "runner_nifty.py"; pkill -f ...
#
# Triggered by Task Scheduler task `Hawala-PkillAll` at 03:30 IST every weekday.

$patterns = @(
    "runner_nifty.py",
    "runner_banknifty.py",
    "news.runner",
    "alerts.vp_live_daemon",
    "alerts.option_flow_daemon",
    "alerts.tick_recorder",
    "alerts.index_1m_intraday",
    "alerts.vp_paper_executor",
    "viewer.live_server"
    # NOTE: alerts.vp_paper_journal is one-shot — no need to kill
)

$killed = 0
foreach ($p in $patterns) {
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*$p*"
    }
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "killed pid=$($proc.ProcessId) ($p)"
            $killed++
        } catch {
            Write-Host "could not kill pid=$($proc.ProcessId) ($p): $_"
        }
    }
}
Write-Host "windows_pkill: $killed processes terminated"
