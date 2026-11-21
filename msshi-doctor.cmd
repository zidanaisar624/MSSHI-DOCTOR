@echo off
setlocal
set "APP_DIR=%~dp0"
if exist "%APP_DIR%msshi-doctor-cli.exe" goto :portable_cli
if not exist "%APP_DIR%src\msshi_doctor_app\cli.py" (
    echo [msshi-doctor] Komponen CLI hilang. Ekstrak kembali seluruh ZIP Doctor ke satu folder. >&2
    exit /b 1
)
set "PYTHONPATH=%APP_DIR%src;%PYTHONPATH%"

where python >nul 2>&1 && goto :python_cli
where py >nul 2>&1 && goto :py_cli

echo [msshi-doctor] Python 3.10+ tidak ditemukan di PATH. >&2
exit /b 1

:portable_cli
"%APP_DIR%msshi-doctor-cli.exe" %*
exit /b %ERRORLEVEL%

:python_cli
python -B -X utf8 -m msshi_doctor_app.cli %*
exit /b %ERRORLEVEL%

:py_cli
py -3 -B -X utf8 -m msshi_doctor_app.cli %*
exit /b %ERRORLEVEL%
