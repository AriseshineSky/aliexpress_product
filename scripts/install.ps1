#Requires -Version 5.1
<#
.SYNOPSIS
  Install aliexpress_product crawler on Windows.
  Auto-installs Python 3.12 via winget when missing.
#>
param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> aliexpress_product installer (Windows)"
Write-Host "Project: $Root"

function Refresh-Path {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Get-PythonCommand {
    param([string]$Preferred)
    Refresh-Path
    $candidates = @()
    if ($Preferred) { $candidates += ,@($Preferred) }
    $candidates += ,@("py", "-3.12")
    $candidates += ,@("py", "-3.11")
    $candidates += ,@("py", "-3.10")
    $candidates += ,@("py", "-3")
    $candidates += ,@("python")
    $candidates += ,@("python3")

    # Common install locations if PATH not refreshed yet
    $localPrograms = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPrograms) {
        Get-ChildItem $localPrograms -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object {
                $exe = Join-Path $_.FullName "python.exe"
                if (Test-Path $exe) { $candidates += ,@($exe) }
            }
    }
    foreach ($ver in @("312", "311", "310")) {
        $pf = Join-Path ${env:ProgramFiles} "Python$ver\python.exe"
        if (Test-Path $pf) { $candidates += ,@($pf) }
    }

    foreach ($cmd in $candidates) {
        try {
            $args = @()
            if ($cmd.Length -gt 1) { $args = $cmd[1..($cmd.Length - 1)] }
            $versionText = & $cmd[0] @args -c "import sys; print(sys.version_info[:2])" 2>$null
            if (-not $versionText) { continue }
            if ($versionText -match "\(3,\s*(\d+)\)") {
                $minor = [int]$Matches[1]
                if ($minor -ge 10) {
                    return ,$cmd
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Install-Python312 {
    Write-Host "Python 3.10+ not found. Trying winget install..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Error @"
Python 3.10+ not found and winget is unavailable.

Install Python manually:
  1. Open https://www.python.org/downloads/
  2. Install Python 3.12
  3. CHECK 'Add python.exe to PATH'
  4. Close this window, open a NEW cmd, run install.bat again
"@
    }

    Write-Host "Installing Python.Python.3.12 via winget (may take a few minutes)..."
    & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
    Start-Sleep -Seconds 2
    Refresh-Path
}

$pythonCmd = Get-PythonCommand -Preferred $PythonExe
if (-not $pythonCmd) {
    Install-Python312
    $pythonCmd = Get-PythonCommand -Preferred $PythonExe
}
if (-not $pythonCmd) {
    Write-Error @"
Python 3.10+ still not found after winget install.

Close this window, open a NEW Command Prompt, then run:
  cd /d C:\aliexpress_product
  install.bat
"@
}

$pyArgs = @()
if ($pythonCmd.Length -gt 1) { $pyArgs = $pythonCmd[1..($pythonCmd.Length - 1)] }
$display = & $pythonCmd[0] @pyArgs -c "import sys; print(sys.version.split()[0], sys.executable)"
Write-Host "Python: $display"

if (-not (Test-Path ".venv")) {
    Write-Host "[1/4] Creating virtual environment .venv"
    & $pythonCmd[0] @pyArgs -m venv .venv
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Virtual environment creation failed: $venvPython not found"
}

Write-Host "[2/4] Installing Python dependencies"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed"
}

Write-Host "[3/4] Installing Playwright Chromium"
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Error "playwright install chromium failed"
}

Write-Host "[4/4] Checking .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host 'Created .env from .env.example - edit it with your credentials before running.'
} else {
    Write-Host '.env already exists (not overwritten).'
}

Write-Host ''
Write-Host 'Install complete. Next steps:'
Write-Host '  1. Ensure .env has ES / Webshare / redis settings'
Write-Host '  2. Run: setup-vps.bat   (9 workers + autostart + 6h reboot)'
Write-Host '  or: start.bat'
