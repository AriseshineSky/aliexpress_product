#Requires -Version 5.1
<#
.SYNOPSIS
  Deploy aliexpress_product on a Windows VPS (RDP shared drive or local).

  - Sync/install crawler with WORKER_COUNT browsers
  - Enable auto-logon so reboot restores an interactive desktop
  - Start crawler at logon (and once now)
  - Schedule OS reboot every N hours (default 6)
#>
param(
    [string]$SourceShare = "\\tsclient\deploy",
    [string]$Dest = "C:\src\aliexpress_product",
    [string]$RepoUrl = "https://github.com/AriseshineSky/aliexpress_product.git",
    [int]$WorkerCount = 9,
    [string]$RedisRole = "both",
    [int]$RebootEveryHours = 6,
    [string]$AutoLogonUser = "",
    [string]$AutoLogonPassword = "",
    [string]$StatusFileName = "deploy-status.txt"
)

$ErrorActionPreference = "Stop"
$LogFile = "C:\deploy-aliexpress.log"
$StatusFile = Join-Path $SourceShare $StatusFileName

function Write-Log {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
    try {
        Set-Content -Path $StatusFile -Value $line -Encoding UTF8
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

function Sync-FromShare {
    param([string]$ShareRoot, [string]$DestRoot)
    $sharePy = Join-Path $ShareRoot "alixq3.py"
    if (-not (Test-Path $sharePy)) {
        return $false
    }
    Write-Log "Syncing project files from shared drive..."
    if (-not (Test-Path $DestRoot)) {
        New-Item -ItemType Directory -Force -Path $DestRoot | Out-Null
    }
    $excludeDirs = @(".venv", "产品详情", "browser_playwright", ".git", ".playwright-mcp", "__pycache__", "img")
    Get-ChildItem -Path $ShareRoot -Force | ForEach-Object {
        if ($excludeDirs -contains $_.Name) { return }
        $target = Join-Path $DestRoot $_.Name
        if ($_.PSIsContainer) {
            robocopy $_.FullName $target /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
        } else {
            Copy-Item $_.FullName $target -Force
        }
    }
    return $true
}

function Enable-AutoLogon {
    param(
        [string]$User,
        [string]$Password
    )
    if ([string]::IsNullOrWhiteSpace($User) -or [string]::IsNullOrWhiteSpace($Password)) {
        Write-Log "Auto-logon skipped (user/password not provided)"
        return
    }
    $winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    Set-ItemProperty -Path $winlogon -Name "AutoAdminLogon" -Value "1" -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultUserName" -Value $User -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultPassword" -Value $Password -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultDomainName" -Value "." -Type String
    # Keep auto-logon permanent across reboots
    Remove-ItemProperty -Path $winlogon -Name "AutoLogonCount" -ErrorAction SilentlyContinue
    Write-Log "Enabled auto-logon for user=$User"
}

function Register-CrawlerLogonTask {
    param(
        [string]$TaskName,
        [string]$StartScript,
        [string]$WorkDir,
        [string]$User
    )
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File `"$StartScript`"" `
        -WorkingDirectory $WorkDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    try { $trigger.Delay = "PT45S" } catch { }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Log "Registered logon task: $TaskName (delay 45s)"
}

function Register-RebootTask {
    param(
        [int]$EveryHours,
        [string]$TaskName = "AliExpressReboot6h"
    )
    if ($EveryHours -le 0) {
        Write-Log "Reboot schedule disabled"
        return
    }
    # Recurring reboot via schtasks (reliable on Server/Win10)
    $tr = "shutdown.exe /r /f /t 90 /c `"AliExpress scheduled recycle every ${EveryHours}h`""
    & schtasks.exe /Create /TN $TaskName /TR $tr /SC HOURLY /MO $EveryHours /RL HIGHEST /RU SYSTEM /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to register reboot task $TaskName (exit=$LASTEXITCODE)"
    }
    Write-Log "Registered reboot task: $TaskName every ${EveryHours}h"
}

function Install-StartupShortcut {
    param(
        [string]$StartScript,
        [string]$WorkDir
    )
    $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    if (-not (Test-Path $startupDir)) {
        New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
    }
    $cmdPath = Join-Path $startupDir "aliexpress-crawler.cmd"
    $content = @"
@echo off
timeout /t 60 /nobreak >nul
cd /d "$WorkDir"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File "$StartScript"
"@
    Set-Content -Path $cmdPath -Value $content -Encoding ASCII
    Write-Log "Installed Startup shortcut: $cmdPath"
}

try {
    Write-Log "=== aliexpress_product VPS deploy started ==="
    Write-Log "Source share: $SourceShare"
    Write-Log "Destination: $Dest"
    Write-Log "WORKER_COUNT=$WorkerCount REDIS_ROLE=$RedisRole RebootEveryHours=$RebootEveryHours"

    if ([string]::IsNullOrWhiteSpace($AutoLogonUser)) {
        $AutoLogonUser = $env:USERNAME
    }

    # Optional password file from share (not committed to git)
    if ([string]::IsNullOrWhiteSpace($AutoLogonPassword)) {
        $pwFile = Join-Path $SourceShare ".autologon-password"
        if (Test-Path $pwFile) {
            $AutoLogonPassword = (Get-Content $pwFile -Raw).Trim()
            Write-Log "Loaded auto-logon password from share"
        }
    }

    Ensure-Command -Name "git" -InstallArgs @("install", "-e", "--id", "Git.Git", "--silent")
    Ensure-Command -Name "python" -InstallArgs @("install", "-e", "--id", "Python.Python.3.12", "--silent")

    $destParent = Split-Path -Parent $Dest
    if (-not (Test-Path $destParent)) {
        New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    }

    $synced = Sync-FromShare -ShareRoot $SourceShare -DestRoot $Dest
    if (-not $synced) {
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

    $envPath = Join-Path $Dest ".env"
    $envLines = Get-Content $envPath | Where-Object {
        $_ -notmatch '^\s*MAX_PRODUCTS\s*=' -and
        $_ -notmatch '^\s*WORKER_COUNT\s*=' -and
        $_ -notmatch '^\s*HEADLESS\s*=' -and
        $_ -notmatch '^\s*REDIS_ROLE\s*='
    }
    $envLines += "WORKER_COUNT=$WorkerCount"
    $envLines += "HEADLESS=0"
    $envLines += "MAX_PRODUCTS=0"
    $envLines += "REDIS_ROLE=$RedisRole"
    Set-Content $envPath ($envLines -join "`n")
    Write-Log "Updated .env: WORKER_COUNT=$WorkerCount HEADLESS=0 MAX_PRODUCTS=0 REDIS_ROLE=$RedisRole"

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'alixq3\.py') } |
        ForEach-Object {
            Write-Log "Stopping previous alixq3.py PID=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }

    Write-Log "Running install.ps1..."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $Dest "scripts\install.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "install.ps1 failed with exit code $LASTEXITCODE"
    }

    Enable-AutoLogon -User $AutoLogonUser -Password $AutoLogonPassword

    $startScript = Join-Path $Dest "scripts\start.ps1"
    Register-CrawlerLogonTask -TaskName "AliExpressProductCrawler" `
        -StartScript $startScript -WorkDir $Dest -User $AutoLogonUser
    Install-StartupShortcut -StartScript $startScript -WorkDir $Dest
    Register-RebootTask -EveryHours $RebootEveryHours

    Write-Log "Starting crawler now with $WorkerCount windows..."
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$startScript`"" `
        -WorkingDirectory $Dest -WindowStyle Normal

    Write-Log "=== Deploy completed successfully ==="
    exit 0
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
