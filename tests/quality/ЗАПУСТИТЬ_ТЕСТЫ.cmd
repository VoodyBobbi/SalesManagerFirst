@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Project Python is missing. Restore .venv with requirements.txt first.
    pause
    exit /b 2
)
".venv\Scripts\python.exe" "tests\quality\runner.py" --repeats 1
set "QA_EXITCODE=%ERRORLEVEL%"
echo.
echo Report: tests\quality\reports\latest.md
pause
exit /b %QA_EXITCODE%