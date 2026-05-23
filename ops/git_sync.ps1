# ops/git_sync.ps1 - Daily safe git-pull on the Windows production box.
#
# Triggered by Task Scheduler `Hawala-GitSync` at 06:50 IST every weekday
# (5 minutes BEFORE autoheal at 06:55). Picks up code changes pushed from
# Mac during the previous day so the production box runs latest code
# without manual `git pull`.
#
# Safety:
#   - Only runs on `main` branch (aborts otherwise)
#   - Uses --ff-only so we NEVER rewrite history
#   - If working tree has uncommitted modifications, abort + log warning
#     (never auto-discards local work)
#   - Untracked files (token.env, caches, logs) are safe - pull doesn't
#     touch them
#   - Exit 0 even on no-op so autoheal at 06:55 still fires
#
# Run manually:
#   cd "D:\Hawala\Hawala v2"
#   powershell -ExecutionPolicy Bypass -File ops\git_sync.ps1
#
# Log:
#   logs\reports\git_sync-YYYYMMDD.log

$ErrorActionPreference = "Continue"

$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $PROJECT_ROOT

$LOG_DIR = Join-Path $PROJECT_ROOT "logs\reports"
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
$LOG = Join-Path $LOG_DIR ("git_sync-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $msg"
    Add-Content -Path $LOG -Value $line
    Write-Host $line
}

Log "========================================"
Log "  git_sync.ps1 starting"
Log "  cwd: $PROJECT_ROOT"

# 1. Verify we are on main
$branch = (git rev-parse --abbrev-ref HEAD) 2>$null
if ($branch -ne "main") {
    Log "  ABORT: on branch '$branch', not 'main' - skipping sync"
    exit 0
}
Log "  on branch: main"

# 2. Refuse to pull if working tree has uncommitted modifications
$dirty = git status --porcelain 2>$null | Where-Object { $_ -match '^[ MADRCU]M ' -or $_ -match '^M ' -or $_ -match '^A ' -or $_ -match '^D ' }
if ($dirty) {
    Log "  ABORT: working tree has uncommitted tracked-file changes:"
    $dirty | ForEach-Object { Log "    $_" }
    Log "  (Untracked files are fine - this only blocks on modified tracked files)"
    Log "  Resolve manually before next sync. Exiting 0 so autoheal still fires."
    exit 0
}

# 3. Fetch + pull (--ff-only never rewrites history)
Log "  git fetch origin ..."
$fetchOutput = git fetch origin 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "  WARN: fetch failed - $fetchOutput"
    exit 0
}

$beforeCommit = (git rev-parse HEAD) 2>$null
Log "  current HEAD: $beforeCommit"

Log "  git pull --ff-only ..."
$pullOutput = git pull --ff-only origin main 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "  WARN: pull failed - $pullOutput"
    Log "  Probably a non-ff (history diverged). Investigate manually."
    exit 0
}

$afterCommit = (git rev-parse HEAD) 2>$null

if ($beforeCommit -eq $afterCommit) {
    Log "  no new commits - already at $afterCommit"
} else {
    Log "  UPDATED: $beforeCommit -> $afterCommit"
    $newCommits = git log "$beforeCommit..$afterCommit" --oneline 2>$null
    Log "  new commits:"
    foreach ($c in $newCommits) { Log "    $c" }

    # NOTE: If any of the new commits touched files used by long-running
    # daemons (runner_*.py, tick_recorder.py, etc.), those daemons are
    # still running the OLD code until tomorrow's 03:30 pkill + next
    # 09:12 launch. Same-day reload would require restarting them by
    # hand or via schtasks /End + /Run. We don't auto-restart because
    # killing mid-trade is risky.
}

Log "  done"
