@echo off
REM Launched by Windows Task Scheduler at 9:10 AM IST on weekdays.
REM Engine handles its own pre-market wait + clean exit at 3:30 PM IST.

cd /d "D:\Projects\AI Stock Market Analyzer"

REM Log all output to a dated file so you can review what happened.
set LOGDIR=D:\Projects\AI Stock Market Analyzer\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "DT=%%a"
set LOGFILE=%LOGDIR%\intraday_%DT:~0,8%.log

python main.py intraday >> "%LOGFILE%" 2>&1
