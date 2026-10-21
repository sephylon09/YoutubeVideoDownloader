@echo off
rem Runs the automated tests. Set MD_NETWORK_TESTS=1 to include real YouTube downloads.
setlocal
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo Run start.bat first to set up the app.
    exit /b 1
)
"%VENV_PY%" -c "import pytest, httpx2" >nul 2>&1 || "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements-dev.txt
"%VENV_PY%" -m pytest %*
