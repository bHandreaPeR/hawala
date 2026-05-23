# windows/IMPORT_TASKS.ps1 - One-shot import of all 16 Hawala scheduled tasks.
#
# Run from PowerShell ADMIN:
#   cd "D:\Hawala\Hawala v2"
#   .\windows\IMPORT_TASKS.ps1
#
# Idempotent - re-running deletes + re-imports each task. Safe to run after
# editing windows\generate_tasks.py + regenerating XMLs.
#
# NOTE: Pure ASCII (no em-dashes). Windows PowerShell 5.1 reads non-BOM UTF-8
# files as Windows-1252 by default; em-dash bytes (E2 80 94) become 'a"' which
# can close PowerShell strings prematurely and cause parse errors.

$ErrorActionPreference = "Stop"
$tasksDir = Join-Path $PSScriptRoot "scheduled_tasks"

if (-not (Test-Path $tasksDir)) {
    Write-Error "No XML dir at $tasksDir - run generate_tasks.py first."
    exit 1
}

# Verify admin
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Must run as Administrator. Right-click PowerShell -> Run as Admin."
    exit 1
}

$xmls = Get-ChildItem -Path $tasksDir -Filter "*.xml"
Write-Host "Importing $($xmls.Count) tasks into Task Scheduler folder \Hawala\ ..." -ForegroundColor Cyan

$ok = 0
$fail = 0
foreach ($f in $xmls) {
    $name = $f.BaseName
    $tn = "\Hawala\$name"
    try {
        schtasks.exe /Delete /TN $tn /F 2>$null | Out-Null
        schtasks.exe /Create /XML $f.FullName /TN $tn /F | Out-Null
        Write-Host "  + $name" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "  X $name : $_" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
$color = if ($fail -eq 0) { "Green" } else { "Yellow" }
Write-Host "Done: $ok ok, $fail failed" -ForegroundColor $color
Write-Host ""
Write-Host "Verify in Task Scheduler GUI (taskschd.msc) under Task Scheduler Library \ Hawala"
Write-Host "Or list via: schtasks /Query /FO TABLE | Select-String Hawala-"
