@echo off
REM Builds the end-of-day journal from the intraday log and emails the digest.
REM Scheduled once per weekday at ~16:15 ET by Windows Task Scheduler.
cd /d "C:\Users\ilyas\OneDrive\Desktop\trade bot"
".venv\Scripts\python.exe" "scripts\eod_report.py" >> "intraday_log\launcher.log" 2>&1
