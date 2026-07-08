@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "\\tsclient\deploy\scripts\deploy-vps.ps1"
echo Deploy finished with exit code %ERRORLEVEL%. See C:\deploy-aliexpress.log
pause
