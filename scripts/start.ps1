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
    Write-Error @"
.venv not found.

Clone only downloads source code. Run the installer first:
  scripts\install.bat
"@
}

if (-not (Test-Path ".env")) {
    Write-Error @"
.env not found.

Copy .env.example to .env and fill in credentials:
  copy .env.example .env
"@
}

$env:PYTHONUNBUFFERED = "1"

Write-Host "==> Starting alixq3.py"
Write-Host "Python: $venvPython"
Write-Host "Working dir: $Root"
Write-Host ""

& $venvPython alixq3.py
exit $LASTEXITCODE
