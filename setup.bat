@echo off
rem ClassHelper - one-time setup: creates .venv and installs dependencies.
title ClassHelper - Setup
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/2] Creating virtual environment .venv ...
    python -m venv .venv
    if %errorlevel% neq 0 ( echo ERROR creating .venv & pause & exit /b 1 )
)

echo [2/2] Installing dependencies (a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 ( echo ERROR installing dependencies & pause & exit /b 1 )

echo.
echo Done. Start the app with run.bat
echo The first launch downloads the Whisper model (~500 MB, only once).
pause
