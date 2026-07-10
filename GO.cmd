@echo off
REM Deploy for VPS with auto-logon password from .autologon-password on the share.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy-vps.ps1" -WorkerCount 9 -RedisRole both -RebootEveryHours 6 -StatusFileName "deploy-status.txt"
echo Deploy finished exit=%ERRORLEVEL%
