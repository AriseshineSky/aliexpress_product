@echo off
setlocal EnableDelayedExpansion
echo [%date% %time%] deploy-shell started > C:\deploy-shell.log
set retries=0
:waitdrive
if exist \\tsclient\deploy\scripts\deploy-vps.ps1 goto run
set /a retries+=1
echo waiting for tsclient drive attempt !retries! >> C:\deploy-shell.log
if !retries! GEQ 90 goto fail
timeout /t 2 >nul
goto waitdrive
:run
echo tsclient drive ready >> C:\deploy-shell.log
powershell -NoProfile -ExecutionPolicy Bypass -File "\\tsclient\deploy\scripts\deploy-vps.ps1" >> C:\deploy-shell.log 2>&1
echo deploy exit code: %ERRORLEVEL% >> C:\deploy-shell.log
start explorer.exe
exit /b 0
:fail
echo tsclient drive not found after 180s >> C:\deploy-shell.log
start explorer.exe
exit /b 1
