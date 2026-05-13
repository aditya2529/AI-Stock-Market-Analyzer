@echo off
REM Starts the dashboard on http://localhost:8000
REM Launched at user logon by Task Scheduler.

cd /d "D:\Projects\AI Stock Market Analyzer"

set LOGDIR=D:\Projects\AI Stock Market Analyzer\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

python main.py dashboard --host 127.0.0.1 >> "%LOGDIR%\dashboard.log" 2>&1
