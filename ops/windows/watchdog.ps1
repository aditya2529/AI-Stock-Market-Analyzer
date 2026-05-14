# Windows watchdog for the intraday engine.
# Runs every 5 min during market hours via Task Scheduler.
# - If engine process missing OR heartbeat file > 10 min stale: alert + restart.
# - Silent outside market hours.

$ErrorActionPreference = "Continue"
$ProjectDir   = "D:\Projects\AI Stock Market Analyzer"
$HeartbeatFile = Join-Path $ProjectDir "logs\heartbeat.txt"
$LogFile       = Join-Path $ProjectDir "logs\watchdog.log"
$LastAlertFile = Join-Path $ProjectDir "logs\watchdog_last_alert.txt"
$TaskName     = "NSE_Intraday_Engine"

function Write-WatchdogLog {
    param([string]$msg)
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $LogFile -Value "[$ts] $msg"
}

function Test-MarketHours {
    $now = Get-Date
    $dow = $now.DayOfWeek
    if ($dow -eq 'Saturday' -or $dow -eq 'Sunday') { return $false }
    $open  = $now.Date.AddHours(9).AddMinutes(15)
    $close = $now.Date.AddHours(15).AddMinutes(30)
    return ($now -ge $open -and $now -le $close)
}

function Test-AlertAllowed {
    if (-not (Test-Path $LastAlertFile)) { return $true }
    $age = (Get-Date) - (Get-Item $LastAlertFile).LastWriteTime
    return ($age.TotalMinutes -gt 30)
}

function Send-TelegramAlert {
    param([string]$msg)
    $envFile = Join-Path $ProjectDir ".env"
    $token = $null
    $chat = $null
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match "^TELEGRAM_BOT_TOKEN=(.+)$") { $token = $Matches[1].Trim() }
            if ($_ -match "^TELEGRAM_CHAT_ID=(.+)$")   { $chat  = $Matches[1].Trim() }
        }
    }
    if (-not $token -or -not $chat) {
        Write-WatchdogLog "Telegram not configured, skipping alert"
        return
    }
    $body = @{ chat_id = $chat; text = $msg; parse_mode = "HTML" } | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 | Out-Null
        Write-WatchdogLog "Telegram alert sent"
        New-Item $LastAlertFile -ItemType File -Force | Out-Null
    } catch {
        Write-WatchdogLog "Telegram send failed: $_"
    }
}

# === main ===
if (-not (Test-MarketHours)) {
    exit 0
}

# Find intraday engine process: python.exe whose command line contains "main.py intraday"
$engineProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*main.py*intraday*" } | Select-Object -First 1

$processAlive  = $null -ne $engineProc
$heartbeatAge  = $null

if (Test-Path $HeartbeatFile) {
    $heartbeatAge = ((Get-Date) - (Get-Item $HeartbeatFile).LastWriteTime).TotalMinutes
}

$problem = $null
if (-not $processAlive) {
    $problem = "Engine process is NOT running"
} elseif ($heartbeatAge -ne $null -and $heartbeatAge -gt 10) {
    $problem = "Heartbeat is $([math]::Round($heartbeatAge,1)) min stale"
}

if (-not $problem) {
    $hbStr = if ($heartbeatAge -ne $null) { [math]::Round($heartbeatAge,1).ToString() + "m" } else { "n/a" }
    Write-WatchdogLog "OK - engine alive (PID $($engineProc.ProcessId)), heartbeat $hbStr"
    exit 0
}

Write-WatchdogLog "PROBLEM: $problem"

if (Test-AlertAllowed) {
    $now = (Get-Date).ToString("HH:mm")
    Send-TelegramAlert "[WATCHDOG] $now IST: $problem. Attempting auto-restart."
}

Write-WatchdogLog "Triggering schtasks /Run for $TaskName"
schtasks /Run /TN $TaskName 2>&1 | ForEach-Object { Write-WatchdogLog $_ }
Start-Sleep -Seconds 8

$engineProc2 = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*main.py*intraday*" } | Select-Object -First 1

if ($engineProc2) {
    Write-WatchdogLog "Restart OK - new PID $($engineProc2.ProcessId)"
    Send-TelegramAlert "[WATCHDOG] Engine restarted successfully (PID $($engineProc2.ProcessId))"
} else {
    Write-WatchdogLog "Restart FAILED - engine still not running"
    Send-TelegramAlert "[WATCHDOG] Engine restart FAILED. Manual intervention needed."
}
