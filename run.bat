@echo off
rem ClassHelper - launch from source (run setup.bat once before).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo .venv not found: run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
if %errorlevel% neq 0 (
    echo.
    echo ClassHelper exited with error code %errorlevel%
    pause
)
