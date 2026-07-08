#Requires -Version 5.1
<#
.SYNOPSIS
  Deploy aliexpress_product on a Windows VPS (run via RDP shared drive or locally).
#>
param(
    [string]$SourceShare = "\\tsclient\deploy",
    [string]$Dest = "C:\src\aliexpress_product",
    [string]$RepoUrl = "https://github.com/AriseshineSky/aliexpress_product.git"
)

$ErrorActionPreference = "Stop"
$LogFile = "C:\deploy-aliexpress.log"
$StatusFile = Join-Path $SourceShare "deploy-status.txt"

function Write-Log {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
    try {
        Set-Content -Path $StatusFile -Value $line
    } catch {
        # tsclient share may be unavailable when run locally
    }
}

function Ensure-Command {
    param(
        [string]$Name,
        [string[]]$InstallArgs
    )
    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        Write-Log "$Name already installed"
        return
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$Name not found and winget unavailable"
    }
    Write-Log "Installing $Name via winget..."
    & winget @InstallArgs --accept-package-agreements --accept-source-agreements
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Failed to install $Name"
    }
}

try {
    Write-Log "=== aliexpress_product VPS deploy started ==="
    Write-Log "Source share: $SourceShare"
    Write-Log "Destination: $Dest"

    Ensure-Command -Name "git" -InstallArgs @("install", "-e", "--id", "Git.Git", "--silent")
    Ensure-Command -Name "python" -InstallArgs @("install", "-e", "--id", "Python.Python.3.12", "--silent")

    $destParent = Split-Path -Parent $Dest
    if (-not (Test-Path $destParent)) {
        New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    }

    if (-not (Test-Path (Join-Path $Dest ".git"))) {
        if (Test-Path $Dest) {
            Write-Log "Removing incomplete destination $Dest"
            Remove-Item -Recurse -Force $Dest
        }
        Write-Log "Cloning repository..."
        & git clone $RepoUrl $Dest
    } else {
        Write-Log "Updating repository..."
        Set-Location $Dest
        & git pull --ff-only
    }

    Set-Location $Dest

    $envSource = Join-Path $SourceShare ".env"
    if (Test-Path $envSource) {
        Write-Log "Copying .env from shared drive"
        Copy-Item $envSource (Join-Path $Dest ".env") -Force
    } elseif (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Log "Created .env from .env.example (no shared .env found)"
    }

    $envLines = Get-Content ".env" | Where-Object {
        $_ -notmatch '^\s*MAX_PRODUCTS\s*=' -and $_ -notmatch '^\s*#.*MAX_PRODUCTS'
    }
    if ($envLines -notmatch '(?m)^\s*WORKER_COUNT\s*=') {
        $envLines += "WORKER_COUNT=2"
    }
    if ($envLines -notmatch '(?m)^\s*HEADLESS\s*=') {
        $envLines += "HEADLESS=0"
    }
    $envLines += "MAX_PRODUCTS=0"
    Set-Content ".env" ($envLines -join "`n")

    Write-Log "Running install.ps1..."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $Dest "scripts\install.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "install.ps1 failed with exit code $LASTEXITCODE"
    }

    $taskName = "AliExpressProductCrawler"
    $startScript = Join-Path $Dest "scripts\start.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`"" -WorkingDirectory $Dest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Log "Registered scheduled task: $taskName"

    Write-Log "Starting crawler now..."
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$startScript`"" -WorkingDirectory $Dest -WindowStyle Normal

    Write-Log "=== Deploy completed successfully ==="
    exit 0
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
