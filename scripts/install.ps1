#Requires -Version 5.1
<#
.SYNOPSIS
  Install aliexpress_product crawler on Windows.
#>
param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> aliexpress_product installer (Windows)"
Write-Host "Project: $Root"

function Get-PythonCommand {
    param([string]$Preferred)
    $candidates = @()
    if ($Preferred) { $candidates += ,@($Preferred) }
    $candidates += ,@("py", "-3.12")
    $candidates += ,@("py", "-3.11")
    $candidates += ,@("py", "-3.10")
    $candidates += ,@("py", "-3")
    $candidates += ,@("python")
    $candidates += ,@("python3")

    foreach ($cmd in $candidates) {
        try {
            $versionText = & $cmd[0] @($cmd[1..($cmd.Length - 1)]) -c "import sys; print(sys.version_info[:2])" 2>$null
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

$pythonCmd = Get-PythonCommand -Preferred $PythonExe
if (-not $pythonCmd) {
    Write-Error "Python 3.10+ not found. Install from https://www.python.org/downloads/ and enable 'Add Python to PATH'."
}

$display = & $pythonCmd[0] @($pythonCmd[1..($pythonCmd.Length - 1)]) -c "import sys; print(sys.version.split()[0], sys.executable)"
Write-Host "Python: $display"

if (-not (Test-Path ".venv")) {
    Write-Host "[1/4] Creating virtual environment .venv"
    & $pythonCmd[0] @($pythonCmd[1..($pythonCmd.Length - 1)]) -m venv .venv
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
    Write-Host "Created .env from .env.example — edit it with your credentials before running."
} else {
    Write-Host ".env already exists (not overwritten)."
}

Write-Host ""
Write-Host "Install complete. Next steps:"
Write-Host "  1. Edit .env with ES / Webshare credentials"
Write-Host "  2. Run: scripts\start.bat"
