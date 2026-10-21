@echo off
rem Updates yt-dlp, its YouTube challenge solver (yt-dlp-ejs) and Deno.
rem YouTube changes often; run this whenever downloads start failing.
setlocal EnableExtensions
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Run start.bat first to set up the app.
    pause
    exit /b 1
)

echo Updating yt-dlp and yt-dlp-ejs...
"%VENV_PY%" -m pip install --disable-pip-version-check --upgrade "yt-dlp[default]"
if errorlevel 1 goto :failed

rem Deno is only present in .venv when a prebuilt wheel exists for this PC.
"%VENV_PY%" -c "import deno" >nul 2>&1
if errorlevel 1 goto :check
echo.
echo Updating Deno...
"%VENV_PY%" -m pip install --disable-pip-version-check --upgrade deno
if errorlevel 1 goto :failed

:check

echo.
"%VENV_PY%" -m media_downloader.doctor
echo.
echo Done. Restart Media Downloader (close its window and run start.bat) to use the new version.
pause
exit /b 0

:failed
echo.
echo Update failed. Check your internet connection and try again.
pause
exit /b 1
