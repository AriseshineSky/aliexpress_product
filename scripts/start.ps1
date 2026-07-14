#Requires -Version 5.1
<#
.SYNOPSIS
  Start AliExpress product detail crawler.

  When .env PROXY_MODE=pool, starts scripts/run_fixed_pool.py (homepage warmup).
  Otherwise starts alixq3.py.
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

# Single-quoted here-string: PowerShell must NOT expand $VAR inside Python code.
$proxyMode = & $venvPython -c @'
from pathlib import Path
try:
    from dotenv import dotenv_values
    vals = dotenv_values(Path(".env"))
except Exception:
    vals = {}
print((vals.get("PROXY_MODE") or "rotate").strip().lower())
'@
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($proxyMode)) {
    $proxyMode = "rotate"
}
$proxyMode = "$proxyMode".Trim()

Write-Host "==> PROXY_MODE=$proxyMode"
Write-Host "Python: $venvPython"
Write-Host "Working dir: $Root"
Write-Host ""

if ($proxyMode -eq "pool") {
    Write-Host "==> Starting scripts/run_fixed_pool.py (homepage warmup + proxy pool)"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $configCheck = & $venvPython -c @'
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(".env"))
os.environ["PROXY_MODE"] = "pool"
from alixq3 import (
    WORKER_COUNT, HEADLESS, SESSION_WARMUP, REDIS_ENABLED, REDIS_ROLE,
    PRODUCT_PACE_SECONDS, POOL_PICK, FIXED_PROXY_POOL, PROXY_FILE,
)
n = len(FIXED_PROXY_POOL)
src = "POOL_PROXIES" if n else f"file:{Path(PROXY_FILE).name}"
print(
    f"WORKER_COUNT={WORKER_COUNT} HEADLESS={HEADLESS} WARMUP={SESSION_WARMUP} "
    f"PACE={PRODUCT_PACE_SECONDS:g}s PICK={POOL_PICK} PROXIES={n or src} "
    f"REDIS={REDIS_ENABLED} ROLE={REDIS_ROLE}"
)
'@ 2>&1
    $configExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($configExit -eq 0) {
        Write-Host "Config: $configCheck"
    } else {
        Write-Warning "Config check failed: $configCheck"
    }
    Write-Host ""
    Write-Host "Tip: seed Windows fingerprints first if queue is empty:"
    Write-Host "  seed-fingerprints.bat 200"
    Write-Host ""
    & $venvPython scripts/run_fixed_pool.py
    exit $LASTEXITCODE
}

Write-Host "==> Starting alixq3.py"
# ContinuePastError: python may write to stderr; do not abort start.ps1.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$configCheck = & $venvPython -c @'
from alixq3 import WORKER_COUNT, HEADLESS, MAX_PRODUCTS, REDIS_ENABLED, REDIS_ROLE, SESSION_WARMUP
print(
    f"WORKER_COUNT={WORKER_COUNT} HEADLESS={HEADLESS} WARMUP={SESSION_WARMUP} "
    f"MAX_PRODUCTS={MAX_PRODUCTS} REDIS={REDIS_ENABLED} ROLE={REDIS_ROLE}"
)
'@ 2>&1
$configExit = $LASTEXITCODE
$ErrorActionPreference = $prevEap

if ($configExit -ne 0) {
    Write-Warning "Config check failed: $configCheck"
} else {
    Write-Host "Config: $configCheck"
    if ("$configCheck" -match 'WORKER_COUNT=1\b') {
        Write-Host "Tip: set WORKER_COUNT in .env to open multiple browser windows."
    }
    if ("$configCheck" -match 'WARMUP=False') {
        Write-Host "Tip: set SESSION_WARMUP=1 in .env to open AliExpress homepage before scraping."
    }
}
Write-Host ""

& $venvPython alixq3.py
exit $LASTEXITCODE
