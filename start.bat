@echo off
rem ==========================================================================
rem  Media Downloader - one-click launcher for Windows
rem
rem  1. Checks for Python 3.10+
rem  2. Creates the .venv virtual environment if needed
rem  3. Installs missing Python packages (FastAPI, yt-dlp[default], Deno, ...)
rem  4. Checks FFmpeg + ffprobe and the JavaScript runtime (offers to install them)
rem  5. Starts the server on http://127.0.0.1:8000 and opens the browser
rem ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Media Downloader

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "STAMP=%VENV_DIR%\requirements.installed"
set "PY_CMD="

echo.
echo  =============================================================
echo     Media Downloader
echo  =============================================================
echo.

rem --------------------------------------------------------------------------
echo  [1/5] Checking Python...
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 goto :venv_ready
    echo        The existing .venv is broken or too old and will be recreated.
)
call :find_python
if not defined PY_CMD goto :no_python
for /f "delims=" %%V in ('%PY_CMD% --version 2^>^&1') do echo        Found %%V

rem --------------------------------------------------------------------------
echo  [2/5] Creating virtual environment in .venv ...
%PY_CMD% -m venv --clear "%VENV_DIR%"
if errorlevel 1 goto :venv_failed
if exist "%STAMP%" del "%STAMP%" >nul 2>&1
goto :deps

:venv_ready
echo  [2/5] Using the existing virtual environment (.venv).

rem --------------------------------------------------------------------------
:deps
echo  [3/5] Checking Python packages...
set "NEED_INSTALL="
if not exist "%STAMP%" set "NEED_INSTALL=1"
if not defined NEED_INSTALL fc /b "requirements.txt" "%STAMP%" >nul 2>&1 || set "NEED_INSTALL=1"
if not defined NEED_INSTALL "%VENV_PY%" -c "import fastapi, uvicorn, jinja2, yt_dlp, yt_dlp_ejs" >nul 2>&1 || set "NEED_INSTALL=1"
if not defined NEED_INSTALL (
    echo        All packages are installed.
    goto :tools
)
echo        Installing packages - this can take a minute on first run...
"%VENV_PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pip_failed
copy /y "requirements.txt" "%STAMP%" >nul

rem --------------------------------------------------------------------------
:tools
echo  [4/5] Checking FFmpeg, ffprobe and the JavaScript runtime...
"%VENV_PY%" -m media_downloader.doctor
set /a "DOCTOR=%errorlevel%"
if %DOCTOR% GEQ 4 if %DOCTOR% LSS 8 goto :pip_failed
set /a "FFMPEG_MISSING=DOCTOR & 1"
set /a "JS_MISSING=(DOCTOR >> 1) & 1"

if "%FFMPEG_MISSING%"=="1" call :offer_ffmpeg
if "%JS_MISSING%"=="1" call :offer_deno
if "%FFMPEG_MISSING%%JS_MISSING%"=="00" goto :launch

echo.
echo  Re-checking...
"%VENV_PY%" -m media_downloader.doctor
set /a "DOCTOR=%errorlevel%"
set /a "FFMPEG_MISSING=DOCTOR & 1"
if "%FFMPEG_MISSING%"=="1" (
    echo.
    echo  WARNING: FFmpeg is still missing. You can analyze videos, but downloads
    echo  will not work until FFmpeg is installed. See README.md for help.
    echo.
    pause
)

rem --------------------------------------------------------------------------
:launch
echo  [5/5] Starting Media Downloader...
echo        Keep this window open while you use the app. Press Ctrl+C to stop.
"%VENV_PY%" app.py --open
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo  The server stopped with an error, exit code %EXIT_CODE%.
    echo  See logs\app.log for details.
    pause
)
endlocal & exit /b %EXIT_CODE%


rem ==========================================================================
rem  Subroutines
rem ==========================================================================

:find_python
rem Prefer the Python launcher (newest installed 3.x), then python on PATH.
rem The Microsoft Store "python" alias fails the version check and is skipped.
for %%C in ("py -3" "python" "python3") do (
    if not defined PY_CMD (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY_CMD=%%~C"
    )
)
exit /b 0

:offer_ffmpeg
echo.
echo  FFmpeg was not found. It is needed to merge video with audio and to create MP3 files.
where winget >nul 2>&1
if errorlevel 1 goto :ffmpeg_manual
choice /c YN /m "  Install FFmpeg now using winget - package Gyan.FFmpeg from gyan.dev"
if errorlevel 2 goto :ffmpeg_manual
winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :ffmpeg_manual
echo  FFmpeg installed.
exit /b 0

:ffmpeg_manual
echo.
echo  To install FFmpeg manually:
echo    1. Open https://ffmpeg.org/download.html and choose the Windows builds from gyan.dev,
echo       or go directly to https://www.gyan.dev/ffmpeg/builds/
echo    2. Download "ffmpeg-release-essentials.zip" and extract it.
echo    3. Either copy ffmpeg.exe and ffprobe.exe from its "bin" folder into:
echo         %~dp0tools\ffmpeg\bin\
echo       or add that "bin" folder to your PATH.
echo    4. Run start.bat again.
echo.
exit /b 0

:offer_deno
echo.
echo  No JavaScript runtime was found. yt-dlp needs Deno to unlock all YouTube formats.
choice /c YN /m "  Install Deno into this app's .venv now using pip"
if errorlevel 2 goto :deno_manual
"%VENV_PY%" -m pip install --disable-pip-version-check "deno>=2.6.6"
if errorlevel 1 goto :deno_manual
exit /b 0

:deno_manual
echo.
echo  To install Deno manually, either run:
echo      winget install --id DenoLand.Deno -e
echo  or follow https://docs.deno.com/runtime/getting_started/installation/
echo  then run start.bat again. Node.js 22+ also works as a fallback.
echo.
exit /b 0

:no_python
echo.
echo  ERROR: Python 3.10 or newer was not found.
echo.
echo    1. Download Python from https://www.python.org/downloads/windows/
echo    2. Run the installer and tick "Add python.exe to PATH".
echo    3. Open a NEW terminal and check that "py --version" prints the version.
echo    4. Run start.bat again.
echo.
pause
exit /b 1

:venv_failed
echo.
echo  ERROR: Could not create the virtual environment in .venv
echo  Delete the .venv folder and try again. If it keeps failing, reinstall Python.
echo.
pause
exit /b 1

:pip_failed
echo.
echo  ERROR: Installing the Python packages failed.
echo  Check your internet connection, firewall or proxy, then run start.bat again.
echo  The full pip output is shown above.
echo.
pause
exit /b 1
