@echo off
rem The gold trader (IBKR paper account). Open IB Gateway, log in to your PAPER account, then double-click this.
rem It checks gold every 5 seconds until you close this window. Output also goes to gold.log in this folder.
rem If it stops (IB Gateway restarted, internet dropped), it starts again by itself after 30 seconds.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
:again
echo ==== %date% %time% ==== >> gold.log
python -m trend_bot gold --discord %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath gold.log -Append"
echo The gold trader stopped. Starting again in 30 seconds (close this window to stop for good).
timeout /t 30 /nobreak >nul
goto again
