@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy-vps.ps1"
echo Deploy finished with exit code %ERRORLEVEL%. See C:\deploy-aliexpress.log
