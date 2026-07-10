@echo off
setlocal EnableDelayedExpansion
set LOG=C:\aliexpress-bootstrap.log
echo [%date% %time%] bootstrap start > "%LOG%"

set SHARE=\\tsclient\deploy
set DEST=C:\aliexpress_product
set STATUS=%SHARE%\deploy-status-vps1.txt

echo [%date% %time%] waiting for share >> "%LOG%"
set retries=0
:waitshare
if exist "%SHARE%\scripts\setup-vps.ps1" goto sync
set /a retries+=1
echo waiting share !retries! >> "%LOG%"
if !retries! GEQ 90 (
  echo ERROR: share not found > "%STATUS%"
  echo ERROR: share not found >> "%LOG%"
  exit /b 1
)
timeout /t 2 >nul
goto waitshare

:sync
echo [%date% %time%] syncing files >> "%LOG%"
echo Syncing from share... > "%STATUS%"
if not exist "%DEST%" mkdir "%DEST%"
robocopy "%SHARE%" "%DEST%" /E /XD .venv "产品详情" browser_playwright .git .playwright-mcp __pycache__ img /NFL /NDL /NJH /NJS /nc /ns /np
if exist "%SHARE%\.env" copy /Y "%SHARE%\.env" "%DEST%\.env" >> "%LOG%" 2>&1
if exist "%SHARE%\.autologon-password" copy /Y "%SHARE%\.autologon-password" "%DEST%\.autologon-password" >> "%LOG%" 2>&1

echo [%date% %time%] ensuring python >> "%LOG%"
echo Installing Python if needed... > "%STATUS%"
where python >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if errorlevel 1 (
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent >> "%LOG%" 2>&1
  )
)

REM Refresh PATH in this cmd session
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MACHINE_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
set "PATH=%MACHINE_PATH%;%USER_PATH%;%PATH%"

cd /d "%DEST%"
echo [%date% %time%] running setup-vps.ps1 >> "%LOG%"
echo Running setup-vps.ps1... > "%STATUS%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEST%\scripts\setup-vps.ps1" -WorkerCount 9 -RedisRole both -RebootEveryHours 6 >> "%LOG%" 2>&1
set ERR=%ERRORLEVEL%
echo [%date% %time%] setup exit=%ERR% >> "%LOG%"
if %ERR% NEQ 0 (
  echo ERROR: setup-vps.ps1 failed exit=%ERR% > "%STATUS%"
  exit /b %ERR%
)

echo Deploy completed successfully > "%STATUS%"
echo [%date% %time%] DONE >> "%LOG%"
exit /b 0
