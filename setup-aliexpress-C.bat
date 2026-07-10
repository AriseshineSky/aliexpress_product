@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title AliExpress Crawler - One Click Setup
cd /d C:\

set "REPO=https://github.com/AriseshineSky/aliexpress_product.git"
set "DEST=C:\aliexpress_product"
set "LOG=C:\aliexpress-setup.log"
set "WORKER_COUNT=9"
set "REDIS_ROLE=both"
set "REBOOT_HOURS=6"
set "TMPDIR=%TEMP%\aliexpress-setup"
if not exist "%TMPDIR%" mkdir "%TMPDIR%"

echo ============================================================
echo  AliExpress Product Crawler - One Click Setup
echo  Target: %DEST%
echo  Workers: %WORKER_COUNT%  ^|  Reboot every %REBOOT_HOURS%h
echo ============================================================
echo.
echo Log: %LOG%
echo [%date% %time%] === setup start === > "%LOG%"

call :RefreshPath

REM ---------- 1) Git ----------
echo [1/7] Checking Git...
where git >nul 2>&1
if errorlevel 1 if exist "C:\Program Files\Git\cmd\git.exe" set "PATH=C:\Program Files\Git\cmd;%PATH%"
where git >nul 2>&1
if errorlevel 1 (
  echo Git not found. Downloading installer ^(no winget needed^)...
  call :InstallGit
  call :RefreshPath
)
where git >nul 2>&1
if errorlevel 1 if exist "C:\Program Files\Git\cmd\git.exe" set "PATH=C:\Program Files\Git\cmd;%PATH%"
where git >nul 2>&1
if errorlevel 1 (
  echo ERROR: Git install failed. See %LOG%
  pause
  exit /b 1
)
git --version
echo.

REM ---------- 2) Python ----------
echo [2/7] Checking Python 3.10+...
call :FindPython
if "!PYEXE!"=="" (
  echo Python not found. Downloading Python 3.12 installer ^(no winget needed^)...
  call :InstallPython
  call :RefreshPath
  call :FindPython
)
if "!PYEXE!"=="" (
  echo ERROR: Python install failed. See %LOG%
  echo Tip: reboot or open a NEW Administrator cmd and re-run this bat.
  pause
  exit /b 1
)
echo Using: !PYEXE!
"!PYEXE!" -c "import sys; print(sys.version)"
echo.

REM ---------- 3) Clone / update / keep existing ----------
echo [3/7] Preparing project folder...
if exist "%DEST%\alixq3.py" (
  echo Found existing %DEST% ^(keep files^)
  if exist "%DEST%\.git\" (
    pushd "%DEST%"
    git pull --ff-only >> "%LOG%" 2>&1
    if errorlevel 1 echo WARNING: git pull failed, continue with local files.
    popd
  )
) else if exist "%DEST%\.git\" (
  pushd "%DEST%"
  git pull --ff-only >> "%LOG%" 2>&1
  popd
) else (
  if exist "%DEST%\" (
    echo Backing up old folder to %DEST%.bak ...
    if exist "%DEST%.bak\" rmdir /s /q "%DEST%.bak"
    move "%DEST%" "%DEST%.bak" >> "%LOG%" 2>&1
  )
  echo Cloning %REPO% ...
  git clone "%REPO%" "%DEST%" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: git clone failed. Check network / GitHub access. See %LOG%
    if exist "%DEST%.bak\alixq3.py" (
      echo Restoring backup folder...
      move "%DEST%.bak" "%DEST%" >> "%LOG%" 2>&1
    )
    pause
    exit /b 1
  )
  if exist "%DEST%.bak\.env" copy /Y "%DEST%.bak\.env" "%DEST%\.env" >nul
)
echo.

REM ---------- 4) .env ----------
echo [4/7] Checking .env...
if not exist "%DEST%\.env" (
  if exist "%DEST%\.env.example" (
    copy /Y "%DEST%\.env.example" "%DEST%\.env" >nul
    echo Created .env from .env.example — please fill credentials.
    notepad "%DEST%\.env"
  ) else (
    echo ERROR: no .env and no .env.example
    pause
    exit /b 1
  )
) else (
  echo .env already exists ^(kept^)
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='%DEST%\.env'; $lines=Get-Content $p | Where-Object { $_ -notmatch '^\s*(WORKER_COUNT|HEADLESS|MAX_PRODUCTS|REDIS_ROLE)\s*=' }; $lines+='WORKER_COUNT=%WORKER_COUNT%'; $lines+='HEADLESS=0'; $lines+='MAX_PRODUCTS=0'; $lines+='REDIS_ROLE=%REDIS_ROLE%'; Set-Content $p ($lines -join \"`n\")"
echo .env workers set to %WORKER_COUNT%
echo.

REM ---------- 5) venv + deps + playwright ----------
echo [5/7] Creating venv and installing dependencies (several minutes)...
cd /d "%DEST%"
if not exist "%DEST%\.venv\Scripts\python.exe" (
  "!PYEXE!" -m venv .venv >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: venv create failed
    pause
    exit /b 1
  )
)
set "VPY=%DEST%\.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: pip install failed. See %LOG%
  pause
  exit /b 1
)
echo Installing Playwright Chromium...
"%VPY%" -m playwright install chromium >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: playwright install failed. See %LOG%
  pause
  exit /b 1
)
echo Dependencies OK.
echo.

REM ---------- 6) Autologon + scheduled tasks + startup ----------
echo [6/7] Configuring auto-start and 6-hour reboot...
set "AL_PASS="
set /p AL_PASS=Enter this PC Administrator password for auto-logon after reboot (required): 
if "!AL_PASS!"=="" (
  echo WARNING: empty password - auto-logon skipped.
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$w='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'; Set-ItemProperty $w AutoAdminLogon '1' -Type String; Set-ItemProperty $w DefaultUserName $env:USERNAME -Type String; Set-ItemProperty $w DefaultPassword $env:AL_PASS -Type String; Set-ItemProperty $w DefaultDomainName '.' -Type String; Remove-ItemProperty $w AutoLogonCount -ErrorAction SilentlyContinue"
  echo Auto-logon enabled for %USERNAME%
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root='%DEST%'; $start=Join-Path $root 'scripts\start.ps1'; $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File \"'+$start+'\"') -WorkingDirectory $root; $trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; try{$trigger.Delay='PT45S'}catch{}; $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero); $principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest; Register-ScheduledTask -TaskName 'AliExpressProductCrawler' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null"
echo Scheduled task: AliExpressProductCrawler (at logon)

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%STARTUP%" mkdir "%STARTUP%"
(
  echo @echo off
  echo timeout /t 60 /nobreak ^>nul
  echo cd /d "%DEST%"
  echo powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File "%DEST%\scripts\start.ps1"
) > "%STARTUP%\aliexpress-crawler.cmd"
echo Startup shortcut installed.

schtasks /Create /TN "AliExpressReboot6h" /TR "shutdown.exe /r /f /t 90 /c AliExpress scheduled recycle every %REBOOT_HOURS%h" /SC HOURLY /MO %REBOOT_HOURS% /RL HIGHEST /RU SYSTEM /F >> "%LOG%" 2>&1
echo Reboot task: every %REBOOT_HOURS% hours
echo.

REM ---------- 7) Start crawler now ----------
echo [7/7] Starting crawler with %WORKER_COUNT% browser windows...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -match 'alixq3\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File "%DEST%\scripts\start.ps1"

echo.
echo ============================================================
echo  DONE
echo  Project:  %DEST%
echo  Log:      %LOG%
echo  Workers:  %WORKER_COUNT%
echo  Reboot:   every %REBOOT_HOURS%h
echo ============================================================
echo [%date% %time%] === setup done === >> "%LOG%"
pause
exit /b 0

REM ===================== helpers =====================
:RefreshPath
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MACHINE_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
set "PATH=%MACHINE_PATH%;%USER_PATH%;%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Python311;%ProgramFiles%\Python312;C:\Program Files\Git\cmd;C:\Program Files\Git\bin"
exit /b 0

:InstallGit
REM Prefer winget; fallback to official Git for Windows silent installer
where winget >nul 2>&1
if not errorlevel 1 (
  winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements --silent >> "%LOG%" 2>&1
  exit /b 0
)
echo Downloading Git for Windows...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; $url='https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe'; $out=Join-Path $env:TEMP 'aliexpress-setup\Git-64-bit.exe'; New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null; Write-Host ('URL: '+$url); Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing; if(-not (Test-Path $out)){ throw 'Git download failed' }; Write-Host 'Installing Git silently...'; $p=Start-Process -FilePath $out -ArgumentList '/VERYSILENT','/NORESTART','/NOCANCEL','/SP-','/CLOSEAPPLICATIONS','/COMPONENTS=icons,ext\reg\shellhere,assoc,assoc_sh' -Wait -PassThru; exit $p.ExitCode" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Git download/install failed. Check network to github.com
  exit /b 1
)
exit /b 0

:InstallPython
where winget >nul 2>&1
if not errorlevel 1 (
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent >> "%LOG%" 2>&1
  exit /b 0
)
echo Downloading Python 3.12...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; $url='https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe'; $out=Join-Path $env:TEMP 'aliexpress-setup\python-3.12.8-amd64.exe'; New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null; Write-Host ('URL: '+$url); Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing; if(-not (Test-Path $out)){ throw 'Python download failed' }; Write-Host 'Installing Python silently...'; $p=Start-Process -FilePath $out -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0','Include_pip=1' -Wait -PassThru; exit $p.ExitCode" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Python download/install failed. Check network to python.org
  exit /b 1
)
exit /b 0

:FindPython
set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  exit /b 0
)
if exist "%ProgramFiles%\Python312\python.exe" (
  set "PYEXE=%ProgramFiles%\Python312\python.exe"
  exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  exit /b 0
)
where python >nul 2>&1 && for /f "delims=" %%P in ('where python') do (
  "%%P" -c "import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PYEXE=%%P"
    exit /b 0
  )
)
where py >nul 2>&1 && (
  for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do (
    set "PYEXE=%%P"
    exit /b 0
  )
)
exit /b 0
