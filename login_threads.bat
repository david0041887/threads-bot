@echo off
REM One-time Threads login for the worker's dedicated Chrome profile.
REM A browser window will open - log in there, then press Enter in this window.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
python local_worker.py --login
echo.
pause >nul
