@echo off
:: Monthly model retrain — fires on 1st of each month at 07:00 IST (01:30 UTC)
:: Refreshes all 25 NSE symbols + macro data, then retrains ensemble

cd /D "D:\Projects\AI Stock Market Analyzer"
echo [%date% %time%] Starting monthly retrain >> logs\retrain.log 2>&1
"D:\Python\python.exe" main.py fetch --market nse --years 10 >> logs\retrain.log 2>&1
"D:\Python\python.exe" main.py fetch --symbol ^NSEI,^INDIAVIX --years 10 >> logs\retrain.log 2>&1
"D:\Python\python.exe" main.py train --all >> logs\retrain.log 2>&1
echo [%date% %time%] Retrain complete >> logs\retrain.log 2>&1
