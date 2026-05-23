# viewer/launch.ps1 — Open the live footprint viewer in a native-feel Chrome
# app window (no tabs, no URL bar). Server should already be running via the
# Hawala-Viewer scheduled task (or launch manually with `python -m viewer.live_server`).

$ErrorActionPreference = "Continue"
$port = if ($env:HAWALA_VIEWER_PORT) { $env:HAWALA_VIEWER_PORT } else { 8765 }
$url  = "http://127.0.0.1:$port/"

# Wait briefly for server to respond
for ($i = 1; $i -le 5; $i++) {
    try {
        $null = Invoke-WebRequest -Uri ($url + "config") -TimeoutSec 1 -UseBasicParsing
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}

# Find Chrome
$chrome = $null
foreach ($p in @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "${env:LocalAppData}\Google\Chrome\Application\chrome.exe"
)) {
    if (Test-Path $p) { $chrome = $p; break }
}
if (-not $chrome) {
    Write-Warning "Chrome not found — opening in default browser instead."
    Start-Process $url
    exit 0
}

$profileDir = Join-Path $env:USERPROFILE ".hawala_viewer_profile"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Start-Process -FilePath $chrome -ArgumentList @(
    "--app=$url",
    "--user-data-dir=$profileDir",
    "--window-size=1400,900",
    "--no-first-run",
    "--no-default-browser-check"
)

Write-Host "viewer launched at $url"
