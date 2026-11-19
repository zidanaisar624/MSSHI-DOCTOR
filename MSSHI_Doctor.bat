@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHONPATH=%APP_DIR%src;%PYTHONPATH%"

where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" pythonw -B -X utf8 -m msshi_doctor_app --gui %*
  exit /b 0
)

where pyw >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" pyw -3 -B -X utf8 -m msshi_doctor_app --gui %*
  exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -B -X utf8 -m msshi_doctor_app --gui %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -B -X utf8 -m msshi_doctor_app --gui %*
  exit /b %ERRORLEVEL%
)

echo [MSSHI Doctor] Python 3.10+ tidak ditemukan di PATH.
pause
exit /b 1
