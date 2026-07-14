@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run install.bat first.
  exit /b 1
)

set COUNT=%~1
if "%COUNT%"=="" set COUNT=200

echo Seeding %COUNT% Windows fingerprints to Redis...
".venv\Scripts\python.exe" scripts\seed_fingerprints_redis.py --count %COUNT% --diverse
exit /b %ERRORLEVEL%
