@echo off
setlocal
cd /d "%~dp0"
call scripts\start.bat %*
exit /b %ERRORLEVEL%
