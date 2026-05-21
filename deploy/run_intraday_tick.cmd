@echo off
REM Runs one iteration of the intraday monitor (tick mode). Scheduled to
REM repeat every few minutes during market hours by Windows Task Scheduler.
REM The script self-gates on real ET market phase, so off-hours ticks no-op.
cd /d "C:\Users\ilyas\OneDrive\Desktop\trade bot"
".venv\Scripts\python.exe" "scripts\intraday_monitor.py" tick >> "intraday_log\launcher.log" 2>&1
