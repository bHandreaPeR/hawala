# =============================================================================
# v3/scripts/weekly_backfill.ps1
# =============================================================================
# Windows port of v3/scripts/weekly_backfill.sh.
# Groww returns OI only for expired contracts; this script re-fetches the
# dates where OI is NaN and contract has since expired — restoring OI.
#
# Triggered by Task Scheduler `Hawala-WeeklyBackfill` Sunday 02:30 IST.
# =============================================================================

$ErrorActionPreference = "Continue"
$SCRIPT_DIR   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $SCRIPT_DIR)
$LOG_DIR      = Join-Path $PROJECT_ROOT "v3\logs"
$TIMESTAMP    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$PYTHON       = "C:\anaconda3\python.exe"
$DRY_RUN      = if ($args -contains "--dry-run") { "--dry-run" } else { "" }

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
Set-Location $PROJECT_ROOT

Write-Host "============================================================"
Write-Host "  weekly_backfill.ps1  |  $TIMESTAMP   $DRY_RUN"
Write-Host "============================================================"

& $PYTHON v3\data\backfill_expired_contracts.py --instrument ALL $DRY_RUN

if ($LASTEXITCODE -eq 0) {
    Write-Host "weekly_backfill.ps1 complete  $(Get-Date -Format "HH:mm:ss")"
} else {
    Write-Host "weekly_backfill.ps1 FAILED (exit=$LASTEXITCODE)" -ForegroundColor Red
}
