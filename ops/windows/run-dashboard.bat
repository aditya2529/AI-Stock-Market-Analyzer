@echo off
REM Starts the dashboard on http://localhost:8000
REM Launched at user logon by Task Scheduler.

cd /d "D:\Projects\AI Stock Market Analyzer"

set LOGDIR=D:\Projects\AI Stock Market Analyzer\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Bind to 0.0.0.0 so Tailscale-connected devices (phone, etc.) can reach the
REM dashboard. Tailnet ACLs already restrict access to your devices only.
python main.py dashboard --host 0.0.0.0 >> "%LOGDIR%\dashboard.log" 2>&1
