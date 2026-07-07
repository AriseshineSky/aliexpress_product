#Requires -Version 5.1
<#
.SYNOPSIS
  Start AliExpress product detail crawler (alixq3.py).
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error @'
.venv not found.

Clone only downloads source code. Run the installer first:
  install.bat
'@
}

if (-not (Test-Path ".env")) {
    Write-Error @'
.env not found.

Copy .env.example to .env and fill in credentials:
  copy .env.example .env
'@
}

$env:PYTHONUNBUFFERED = "1"

Write-Host "==> Starting alixq3.py"
Write-Host "Python: $venvPython"
Write-Host "Working dir: $Root"
Write-Host ""

$configCheck = & $venvPython -c "from alixq3 import WORKER_COUNT, HEADLESS, MAX_PRODUCTS; print(f'WORKER_COUNT={WORKER_COUNT} HEADLESS={HEADLESS} MAX_PRODUCTS={MAX_PRODUCTS}')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Config check failed: $configCheck"
} else {
    Write-Host "Config: $configCheck"
    if ($configCheck -match 'WORKER_COUNT=1\b') {
        Write-Host "Tip: set WORKER_COUNT=2 in .env to open multiple browser windows."
    }
}
Write-Host ""

& $venvPython alixq3.py
exit $LASTEXITCODE
