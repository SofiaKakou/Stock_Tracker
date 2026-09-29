@echo off
rem Runs the daily update + Discord alert. Windows Task Scheduler calls this every weekday.
rem Output (including errors) is appended to alerts.log in this folder.
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==== %date% %time% ==== >> alerts.log
if exist market.db (
    python -m trend_bot db update >> alerts.log 2>&1
    python -m trend_bot alert --summary --market >> alerts.log 2>&1
    python -m trend_bot report >> alerts.log 2>&1
) else (
    python -m trend_bot alert --summary >> alerts.log 2>&1
)
