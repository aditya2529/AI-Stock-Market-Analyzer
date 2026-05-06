# setup_scheduler.ps1
# Registers two Windows Scheduled Tasks for AI Stock Analyzer.
# Run once as Administrator:  .\scripts\setup_scheduler.ps1
#
# Tasks created:
#   AI-StockAnalyzer-PaperEOD    — daily Mon-Fri at 15:35 IST (paper trading EOD pass)
#   AI-StockAnalyzer-Retrain     — 1st of every month at 07:00 IST (model retrain)

$ProjectDir = "D:\Projects\AI Stock Market Analyzer"
$PaperBat   = "$ProjectDir\scripts\run_paper_eod.bat"
$RetrainBat = "$ProjectDir\scripts\run_monthly_retrain.bat"

# ── Helper ──────────────────────────────────────────────────────────────
function Register-Task {
    param($Name, $BatPath, $Trigger, $Description)
    $Action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`""
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType InteractiveToken `
        -RunLevel Highest

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  Replaced existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName    $Name `
        -Action      $Action `
        -Trigger     $Trigger `
        -Settings    $Settings `
        -Principal   $Principal `
        -Description $Description | Out-Null

    Write-Host "  Registered: $Name"
}

Write-Host "`nAI Stock Analyzer — Scheduler Setup"
Write-Host "====================================`n"

# ── Task 1: Daily EOD paper run (Mon-Fri 15:35 IST) ────────────────────
# 15:35 IST = 10:05 UTC. Windows Task Scheduler uses LOCAL time.
# We use 15:35 and assume the machine is set to IST (UTC+5:30).
# If your machine is NOT on IST, change the time below to your local equivalent of 15:35 IST.
$PaperTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "15:35"

Register-Task `
    -Name        "AI-StockAnalyzer-PaperEOD" `
    -BatPath     $PaperBat `
    -Trigger     $PaperTrigger `
    -Description "Daily EOD paper trading pass after NSE close. Generates signals, checks stops/targets, logs PnL, sends Telegram summary."

# ── Task 2: Monthly retrain (1st of month at 07:00 IST) ────────────────
$RetrainTrigger = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At "07:00"

Register-Task `
    -Name        "AI-StockAnalyzer-Retrain" `
    -BatPath     $RetrainBat `
    -Trigger     $RetrainTrigger `
    -Description "Monthly model retrain. Refreshes 10yr OHLCV for all 25 NSE symbols + Nifty/VIX, then retrains the full ensemble."

# ── Summary ─────────────────────────────────────────────────────────────
Write-Host "`nSchedule summary:"
Write-Host "  Paper EOD  : Mon-Fri at 15:35 (after NSE close)"
Write-Host "  Retrain    : 1st of each month at 07:00"
Write-Host "`nTo verify:  Get-ScheduledTask -TaskName 'AI-StockAnalyzer-*' | Format-Table TaskName,State"
Write-Host "To run now: Start-ScheduledTask -TaskName 'AI-StockAnalyzer-PaperEOD'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName 'AI-StockAnalyzer-PaperEOD' -Confirm:`$false"
Write-Host ""
