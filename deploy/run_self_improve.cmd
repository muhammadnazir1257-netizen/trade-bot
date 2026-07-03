@echo off
REM Weekly evidence-gated self-improvement cycle: replay grid + walk-forward
REM tuner + test-suite audit. Only bounded, whitelisted params can change;
REM the risk floor is structurally immune. Scheduled Sundays (TradeBot-SelfImprove).
cd /d "C:\Users\ilyas\OneDrive\Desktop\trade bot"
".venv\Scripts\python.exe" "scripts\self_improve.py" run >> "intraday_log\self_improve.log" 2>&1
