# =============================================================================
# v3/scripts/daily_fetch.ps1
# =============================================================================
# Windows port of v3/scripts/daily_fetch.sh. Same 8 steps in same order.
# Triggered by Task Scheduler `Hawala-DailyFetch` at 16:30 IST weekdays.
#
# To run manually:
#   cd "D:\Hawala\Hawala v2"
#   powershell -ExecutionPolicy Bypass -File v3\scripts\daily_fetch.ps1
# =============================================================================

$ErrorActionPreference = "Continue"
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

$SCRIPT_DIR   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $SCRIPT_DIR)
$LOG_DIR      = Join-Path $PROJECT_ROOT "v3\logs"
$TIMESTAMP    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$PYTHON       = "D:\anaconda3\python.exe"

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
Set-Location $PROJECT_ROOT

Write-Host "============================================================"
Write-Host "  daily_fetch.ps1  |  $TIMESTAMP"
Write-Host "  project: $PROJECT_ROOT"
Write-Host "============================================================"

function Run-Step {
    param([string]$Name, [string]$Script)
    Write-Host ""
    Write-Host "----- $Name -----"
    & $PYTHON $Script
    if ($LASTEXITCODE -ne 0) {
        Write-Host "STEP FAILED (exit=$LASTEXITCODE): $Name — continuing" -ForegroundColor Yellow
    }
}

Run-Step "NIFTY futures 1m candles"      "v3\data\fetch_1m_NIFTY.py"
Run-Step "NIFTY option OI 1m"            "v3\data\fetch_option_oi_NIFTY.py"
Run-Step "BankNifty futures 1m candles"  "v3\data\fetch_1m_BANKNIFTY.py"
Run-Step "BankNifty option OI 1m"        "v3\data\fetch_option_oi_BANKNIFTY.py"
Run-Step "SENSEX 1m candles"             "v3\data\fetch_1m_SENSEX.py"
Run-Step "NSE bhavcopy (NIFTY PCR)"      "v3\data\fetch_bhavcopy_nifty.py"
Run-Step "NSE bhavcopy (BANKNIFTY PCR)"  "v3\data\fetch_bhavcopy_banknifty.py"
Run-Step "Build pcr_daily.csv"           "v3\data\build_pcr_daily.py"
Run-Step "FII Cash (fii_data.csv)"       "v3\data\fetch_fii_cash.py"
Run-Step "FII F&O (_fii_fo_cache.pkl)"   "v3\data\fetch_fii_fo.py"

Write-Host ""
Write-Host "daily_fetch.ps1 complete  $(Get-Date -Format "HH:mm:ss")"
