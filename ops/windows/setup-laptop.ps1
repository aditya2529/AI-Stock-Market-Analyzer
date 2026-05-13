# One-time setup for running the AI Stock Analyzer on Windows.
# Run ONCE in elevated PowerShell (right-click PowerShell -> Run as Administrator):
#     powershell -ExecutionPolicy Bypass -File "D:\Projects\AI Stock Market Analyzer\ops\windows\setup-laptop.ps1"

$ErrorActionPreference = "Stop"
$ProjectDir = "D:\Projects\AI Stock Market Analyzer"
$WinOpsDir  = Join-Path $ProjectDir "ops\windows"
$IntradayBat  = Join-Path $WinOpsDir "run-intraday.bat"
$DashboardBat = Join-Path $WinOpsDir "run-dashboard.bat"

Write-Host "=== AI Stock Analyzer — Windows setup ===" -ForegroundColor Cyan

# 1. Verify project exists
if (-not (Test-Path $IntradayBat)) {
    Write-Error "Cannot find $IntradayBat. Did you pull the latest from git?"
}

# 2. Power settings — never sleep when plugged in, never sleep on battery,
#    never turn off display when plugged in.
Write-Host "`n[1/4] Configuring power settings (no sleep, no hibernate when plugged in)..." -ForegroundColor Yellow
powercfg /change standby-timeout-ac 0       # never sleep on AC
powercfg /change hibernate-timeout-ac 0     # never hibernate on AC
powercfg /change monitor-timeout-ac 0       # display never off on AC
powercfg /change disk-timeout-ac 0          # disk never spin down on AC
Write-Host "   Power: laptop will stay awake when plugged in." -ForegroundColor Green

# 3. Scheduled Task — intraday engine. Fires Mon-Fri at 9:10 AM local time.
#    NOTE: assumes laptop clock is set to IST (Asia/Kolkata). Most Indian laptops are.
Write-Host "`n[2/4] Creating Scheduled Task: NSE Intraday Engine (9:10 AM Mon-Fri)..." -ForegroundColor Yellow
$taskName = "NSE_Intraday_Engine"
schtasks /Delete /TN $taskName /F 2>$null | Out-Null

$trigger = "/SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:10"
$action  = "/TR `"$IntradayBat`""
# /RL HIGHEST so process can write to logs dir without UAC prompt
# /RU $env:USERNAME runs under current user (no password needed for interactive context)
cmd /c "schtasks /Create /TN $taskName $trigger $action /F /RL LIMITED /IT" | Out-Null
Write-Host "   Task '$taskName' created. Fires at 9:10 AM IST on weekdays." -ForegroundColor Green

# 4. Scheduled Task — dashboard at user logon (so it's always up while you're logged in)
Write-Host "`n[3/4] Creating Scheduled Task: NSE Dashboard (at logon)..." -ForegroundColor Yellow
$dashTask = "NSE_Dashboard"
schtasks /Delete /TN $dashTask /F 2>$null | Out-Null
cmd /c "schtasks /Create /TN $dashTask /SC ONLOGON /TR `"$DashboardBat`" /F /RL LIMITED /IT" | Out-Null
Write-Host "   Task '$dashTask' created. Starts automatically when you log in." -ForegroundColor Green

# 5. Verify Python + key deps are present (best-effort sanity check)
Write-Host "`n[4/4] Sanity check: Python + key packages..." -ForegroundColor Yellow
$pyver = (python --version) 2>&1
Write-Host "   $pyver"
$pkgs = python -c "import xgboost, pandas, yfinance, shap; print('all imports OK')" 2>&1
Write-Host "   $pkgs"

Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "What's set up:" -ForegroundColor Cyan
Write-Host "  - Laptop will not sleep when plugged in"
Write-Host "  - Intraday engine auto-starts 9:10 AM Mon-Fri (and exits cleanly at 3:30 PM IST)"
Write-Host "  - Dashboard auto-starts when you log in (http://localhost:8000)"
Write-Host "  - All logs in D:\Projects\AI Stock Market Analyzer\logs\"
Write-Host ""
Write-Host "To check the tasks any time:" -ForegroundColor Yellow
Write-Host "  schtasks /Query /TN $taskName"
Write-Host "  schtasks /Query /TN $dashTask"
Write-Host ""
Write-Host "To run intraday manually right now (test):" -ForegroundColor Yellow
Write-Host "  schtasks /Run /TN $taskName"
