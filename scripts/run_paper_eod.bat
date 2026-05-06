@echo off
:: Daily EOD paper trading run — fires after NSE close (15:35 IST)
:: Scheduled by setup_scheduler.ps1 — do not edit the schedule here

cd /D "D:\Projects\AI Stock Market Analyzer"
"D:\Python\python.exe" main.py paper --once >> logs\paper_eod.log 2>&1
