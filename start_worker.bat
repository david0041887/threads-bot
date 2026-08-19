@echo off
REM Threads bot - local browser worker
REM Keep this window open. Closing it stops the patrol.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo Starting Threads local worker...
python local_worker.py
echo.
echo Worker stopped. Press any key to close.
pause >nul
