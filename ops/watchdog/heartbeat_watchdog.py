#!/usr/bin/env python3
"""Heartbeat watchdog — fires Telegram alert if intraday engine goes silent.

Called by cron every 5 minutes during market hours (3:40-10:15 UTC, Mon-Fri).

The engine writes /home/opc/health/intraday.heartbeat each tick.
If file age > 15 minutes during market hours → alert.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HEARTBEAT_FILE = Path("/home/opc/health/intraday.heartbeat")
MAX_AGE_SECONDS = 15 * 60  # 15 min
ALERT_COOLDOWN_FILE = Path("/home/opc/health/watchdog_last_alert")
ALERT_COOLDOWN_SECONDS = 30 * 60  # don't spam — 1 alert per 30 min

# IST market hours: 9:15 AM - 3:30 PM IST = 03:45 - 10:00 UTC
IST = timezone(timedelta(hours=5, minutes=30))


def is_market_hours() -> bool:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Sat/Sun
        return False
    open_ = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_ = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now_ist <= close_


def can_alert() -> bool:
    if not ALERT_COOLDOWN_FILE.exists():
        return True
    age = time.time() - ALERT_COOLDOWN_FILE.stat().st_mtime
    return age > ALERT_COOLDOWN_SECONDS


def mark_alerted():
    ALERT_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_COOLDOWN_FILE.touch()


def send_telegram(msg: str) -> None:
    """Use the app's existing telegram_bot module."""
    sys.path.insert(0, "/home/opc/app")
    try:
        from alerts.telegram_bot import send_message
        send_message(msg)
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)


def main():
    if not is_market_hours():
        return 0  # silent outside market hours

    if not HEARTBEAT_FILE.exists():
        # Silent — engine hasn't been wired for heartbeats yet, or never started today
        return 0
    else:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
        if age <= MAX_AGE_SECONDS:
            return 0  # healthy
        msg = (f"⚠️ WATCHDOG: intraday engine silent for {age/60:.1f} min "
               f"(threshold {MAX_AGE_SECONDS/60:.0f} min). Check systemd: "
               f"systemctl status nse-intraday")

    if can_alert():
        send_telegram(msg)
        mark_alerted()
        print(f"ALERT SENT: {msg}")
    else:
        print(f"Suppressed (cooldown active): {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
