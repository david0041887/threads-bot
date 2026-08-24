@echo off
REM Threads bot - local browser worker supervisor
REM If Python exits unexpectedly, restart it after 15 seconds.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
:restart
echo [%date% %time%] Starting Threads local worker...
python local_worker.py
set WORKER_EXIT=%ERRORLEVEL%
echo [%date% %time%] Worker exited with code %WORKER_EXIT%. Restarting in 15 seconds...
timeout /t 15 /nobreak >nul
goto restart
