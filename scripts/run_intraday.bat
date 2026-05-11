@echo off
:: Intraday session — auto-starts at 9:15 AM, runs until 3:15 PM, then exits
cd /D "D:\Projects\AI Stock Market Analyzer"
"D:\Python\python.exe" main.py intraday >> logs\intraday.log 2>&1
