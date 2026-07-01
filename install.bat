@echo off
setlocal
cd /d "%~dp0"
call scripts\install.bat %*
exit /b %ERRORLEVEL%
