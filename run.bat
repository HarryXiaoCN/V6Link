@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    set "PYTHON_CMD=python"
) else (
    set "PYTHON_CMD=py -3"
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo [V6Link] First run: creating the Python environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

start "" ".venv\Scripts\pythonw.exe" "main.py" %*
exit /b 0

:fail
echo.
echo [V6Link] Setup failed. Make sure Python 3.11+ is installed and available in PATH.
echo Install dependencies manually with:
echo     %PYTHON_CMD% -m pip install -r requirements.txt
pause
exit /b 1
