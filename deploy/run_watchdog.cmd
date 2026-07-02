@echo off
REM Dead-man's switch: alerts by email if the heartbeat goes stale or the
REM kill switch engages. Scheduled every 30 minutes (TradeBot-Watchdog).
cd /d "C:\Users\ilyas\OneDrive\Desktop\trade bot"
".venv\Scripts\python.exe" "scripts\watchdog.py" check >> "intraday_log\watchdog.log" 2>&1
