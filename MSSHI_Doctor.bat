@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYTHONPATH=%APP_DIR%src;%PYTHONPATH%"

where pythonw >nul 2>&1 && goto :python_gui
where pyw >nul 2>&1 && goto :py_gui
where python >nul 2>&1 && goto :python_console
where py >nul 2>&1 && goto :py_console

echo [MSSHI Doctor] Python 3.10+ tidak ditemukan di PATH.
pause
exit /b 1

:python_gui
start "" pythonw -B -X utf8 -m msshi_doctor_app --gui %*
exit /b %ERRORLEVEL%

:py_gui
start "" pyw -3 -B -X utf8 -m msshi_doctor_app --gui %*
exit /b %ERRORLEVEL%

:python_console
python -B -X utf8 -m msshi_doctor_app --gui %*
exit /b %ERRORLEVEL%

:py_console
py -3 -B -X utf8 -m msshi_doctor_app --gui %*
exit /b %ERRORLEVEL%
