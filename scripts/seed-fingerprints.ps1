#Requires -Version 5.1
<#
.SYNOPSIS
  Generate Windows Chrome fingerprints and push to Redis queue alixq3:fps.

.EXAMPLE
  .\scripts\seed-fingerprints.ps1
  .\scripts\seed-fingerprints.ps1 -Count 500
  .\scripts\seed-fingerprints.ps1 -Status
#>
param(
    [int]$Count = 200,
    [switch]$Status,
    [switch]$NoDiverse
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error ".venv not found. Run install.bat first."
}

$env:PYTHONUNBUFFERED = "1"
$argsList = @("scripts\seed_fingerprints_redis.py")
if ($Status) {
    $argsList += "--status"
} else {
    $argsList += @("--count", "$Count")
    if (-not $NoDiverse) {
        $argsList += "--diverse"
    }
}

Write-Host "==> Seeding Windows fingerprints (count=$Count status=$Status)"
& $venvPython @argsList
exit $LASTEXITCODE
