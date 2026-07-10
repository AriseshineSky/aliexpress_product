#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot VPS setup from an already-copied project folder:
  - ensure install
  - WORKER_COUNT=9 + Redis consumer/both
  - auto-logon + start crawler at logon
  - reboot every 6 hours
  - start crawler now
#>
param(
    [int]$WorkerCount = 9,
    [string]$RedisRole = "both",
    [int]$RebootEveryHours = 6,
    [string]$AutoLogonPassword = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> VPS setup"
Write-Host "Project: $Root"

# Ensure deps installed
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\install.ps1")
if ($LASTEXITCODE -ne 0) { throw "install.ps1 failed" }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

$envLines = Get-Content ".env" | Where-Object {
    $_ -notmatch '^\s*MAX_PRODUCTS\s*=' -and
    $_ -notmatch '^\s*WORKER_COUNT\s*=' -and
    $_ -notmatch '^\s*HEADLESS\s*=' -and
    $_ -notmatch '^\s*REDIS_ROLE\s*='
}
$envLines += "WORKER_COUNT=$WorkerCount"
$envLines += "HEADLESS=0"
$envLines += "MAX_PRODUCTS=0"
$envLines += "REDIS_ROLE=$RedisRole"
Set-Content ".env" ($envLines -join "`n")
Write-Host "Updated .env: WORKER_COUNT=$WorkerCount REDIS_ROLE=$RedisRole"

$user = $env:USERNAME
if ([string]::IsNullOrWhiteSpace($AutoLogonPassword)) {
    foreach ($pwCandidate in @(
        (Join-Path $Root ".autologon-password"),
        (Join-Path $Root "..\.autologon-password")
    )) {
        if (Test-Path $pwCandidate) {
            $AutoLogonPassword = (Get-Content $pwCandidate -Raw).Trim()
            Write-Host "Loaded auto-logon password from file"
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($AutoLogonPassword) -and [Environment]::UserInteractive) {
    try {
        $secure = Read-Host "Enter Windows password for auto-logon after reboot (or leave empty to skip)" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $AutoLogonPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    } catch {
        Write-Host "No interactive password prompt available; skipping auto-logon password"
    }
}

if (-not [string]::IsNullOrWhiteSpace($AutoLogonPassword)) {
    $winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    Set-ItemProperty -Path $winlogon -Name "AutoAdminLogon" -Value "1" -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultUserName" -Value $user -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultPassword" -Value $AutoLogonPassword -Type String
    Set-ItemProperty -Path $winlogon -Name "DefaultDomainName" -Value "." -Type String
    Remove-ItemProperty -Path $winlogon -Name "AutoLogonCount" -ErrorAction SilentlyContinue
    Write-Host "Enabled auto-logon for $user"
} else {
    Write-Host "Skipped auto-logon (no password). After reboot you must log in once for crawler to start."
}

$startScript = Join-Path $Root "scripts\start.ps1"
$taskName = "AliExpressProductCrawler"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File `"$startScript`"" `
    -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
try { $trigger.Delay = "PT45S" } catch { }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered logon task: $taskName"

$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
if (-not (Test-Path $startupDir)) { New-Item -ItemType Directory -Force -Path $startupDir | Out-Null }
$startupCmd = Join-Path $startupDir "aliexpress-crawler.cmd"
@"
@echo off
timeout /t 60 /nobreak >nul
cd /d "$Root"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File "$startScript"
"@ | Set-Content -Path $startupCmd -Encoding ASCII
Write-Host "Installed Startup shortcut"

if ($RebootEveryHours -gt 0) {
    $tr = "shutdown.exe /r /f /t 90 /c `"AliExpress scheduled recycle every ${RebootEveryHours}h`""
    & schtasks.exe /Create /TN "AliExpressReboot6h" /TR $tr /SC HOURLY /MO $RebootEveryHours /RL HIGHEST /RU SYSTEM /F | Out-Null
    Write-Host "Registered reboot every ${RebootEveryHours}h"
}

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'alixq3\.py') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Starting crawler with $WorkerCount windows..."
Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$startScript`"" `
    -WorkingDirectory $Root -WindowStyle Normal

Write-Host ""
Write-Host "=== VPS setup done ==="
Write-Host "Crawler starting now. After reboot it auto-starts (if auto-logon set)."
Write-Host "Machine reboots every ${RebootEveryHours} hours."
