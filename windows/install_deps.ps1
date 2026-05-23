# windows/install_deps.ps1 — Anaconda-friendly Python deps install.
#
# Why: growwapi pins numpy<2.0. Anaconda Python 3.13 ships numpy 2.x. Pip's
# resolver tries to downgrade → no Windows wheels for numpy 1.26 on Py 3.13 →
# source build → no MSVC compiler → install fails.
#
# Fix: install growwapi --no-deps first (it works with numpy 2.x in practice
# despite the conservative pin), then install everything else cleanly.
#
# Run from PowerShell (no admin needed):
#   cd "D:\Hawala\Hawala v2"
#   .\windows\install_deps.ps1

$ErrorActionPreference = "Stop"
$PYTHON = "D:\anaconda3\python.exe"

if (-not (Test-Path $PYTHON)) {
    Write-Error "Python not found at $PYTHON — edit this script if Anaconda is elsewhere."
    exit 1
}

Write-Host "1/2  Installing growwapi (--no-deps so numpy isn't downgraded)..." -ForegroundColor Cyan
& $PYTHON -m pip install --no-deps --upgrade growwapi
if ($LASTEXITCODE -ne 0) { Write-Error "growwapi install failed"; exit 1 }

Write-Host ""
Write-Host "2/2  Installing requirements.txt..." -ForegroundColor Cyan
& $PYTHON -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "requirements.txt install failed"; exit 1 }

Write-Host ""
Write-Host "Verifying critical imports..." -ForegroundColor Cyan
& $PYTHON -c "import growwapi, numpy, pandas, fastapi, pyarrow, plotly, pyotp; print(f'  numpy={numpy.__version__} pandas={pandas.__version__} fastapi={fastapi.__version__}')"
if ($LASTEXITCODE -ne 0) { Write-Error "import check failed"; exit 1 }

Write-Host ""
Write-Host "All deps installed OK." -ForegroundColor Green
