@echo off
rem Runs the Discord alert once. Windows Task Scheduler calls this every weekday.
rem Output (including errors) is appended to alerts.log in this folder.
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==== %date% %time% ==== >> alerts.log
python -m trend_bot alert --summary >> alerts.log 2>&1
