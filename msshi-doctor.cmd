@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHONPATH=%APP_DIR%src;%PYTHONPATH%"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -B -X utf8 -m msshi_doctor_app.cli %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -B -X utf8 -m msshi_doctor_app.cli %*
  exit /b %ERRORLEVEL%
)

echo [msshi-doctor] Python 3.10+ tidak ditemukan di PATH. >&2
exit /b 1
