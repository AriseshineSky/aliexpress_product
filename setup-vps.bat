@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-vps.ps1" %*
echo.
echo setup-vps exit code: %ERRORLEVEL%
pause
exit /b %ERRORLEVEL%
