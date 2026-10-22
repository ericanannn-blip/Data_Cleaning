@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python start_local.py %*
) else (
    py -3.12 start_local.py %*
)
if errorlevel 1 (
    echo.
    echo Startup failed. See LOCAL_START.md; Python 3.12 is required.
    pause
)
