"""System health & ops endpoint — for the Health tab in the dashboard.

Gathers vitals about the intraday engine, VPS, pipeline, and recent errors.
Designed to be cheap (~200ms) so the frontend can poll it every 10 sec.
"""
from __future__ import annotations
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])

# Paths
HEARTBEAT_FILE = Path("/home/opc/health/intraday.heartbeat")
INTRADAY_LOG   = Path("/home/opc/logs/intraday.log")
DASHBOARD_LOG  = Path("/home/opc/logs/dashboard.log")

IST = timezone(timedelta(hours=5, minutes=30))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 3.0) -> str:
    """Run a command, return stdout or empty string on any error."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def _systemd_state(unit: str) -> dict:
    """Return active/enabled state + restart count + last log line for a unit."""
    active = _run(["systemctl", "is-active", unit]) or "unknown"
    enabled = _run(["systemctl", "is-enabled", unit]) or "unknown"

    # Property dump for memory + main PID + last restart
    props_raw = _run([
        "systemctl", "show", unit,
        "--property=MemoryCurrent,MainPID,ExecMainStartTimestamp,NRestarts,Result"
    ])
    props = {}
    for line in props_raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v

    mem_bytes = int(props.get("MemoryCurrent", "0") or 0)
    pid = int(props.get("MainPID", "0") or 0)
    start_ts = props.get("ExecMainStartTimestamp", "")
    n_restarts = int(props.get("NRestarts", "0") or 0)
    last_result = props.get("Result", "")

    return {
        "active": active,
        "enabled": enabled,
        "memory_mb": round(mem_bytes / (1024 * 1024), 1) if mem_bytes else 0.0,
        "pid": pid,
        "started_at": start_ts,
        "restarts": n_restarts,
        "last_result": last_result,
    }


def _vps_vitals() -> dict:
    """RAM, swap, disk, uptime, load."""
    out = {}

    # Memory (read /proc/meminfo — works without psutil)
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            v = rest.strip().split()[0]
            info[k] = int(v)  # kB
        out["mem_total_mb"]     = round(info["MemTotal"] / 1024, 0)
        out["mem_available_mb"] = round(info["MemAvailable"] / 1024, 0)
        out["mem_used_mb"]      = round((info["MemTotal"] - info["MemAvailable"]) / 1024, 0)
        out["swap_total_mb"]    = round(info["SwapTotal"] / 1024, 0)
        out["swap_used_mb"]     = round((info["SwapTotal"] - info["SwapFree"]) / 1024, 0)
        out["mem_used_pct"]     = round(out["mem_used_mb"] / out["mem_total_mb"] * 100, 1)
    except Exception as e:
        out["mem_error"] = str(e)

    # Disk on app partition
    try:
        du = shutil.disk_usage("/home/opc/app")
        out["disk_total_gb"] = round(du.total / 1024**3, 1)
        out["disk_free_gb"]  = round(du.free / 1024**3, 1)
        out["disk_used_pct"] = round((du.total - du.free) / du.total * 100, 1)
    except Exception as e:
        out["disk_error"] = str(e)

    # Uptime + load
    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
        out["uptime_hours"] = round(uptime_sec / 3600, 1)
    except Exception:
        out["uptime_hours"] = 0
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        out["load_1m"]  = float(la[0])
        out["load_5m"]  = float(la[1])
        out["load_15m"] = float(la[2])
    except Exception:
        pass

    out["hostname"] = socket.gethostname()
    return out


def _heartbeat() -> dict:
    """Age of last heartbeat write from the engine."""
    if not HEARTBEAT_FILE.exists():
        return {"exists": False, "age_seconds": None, "status": "missing"}
    age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    if age < 600:       # < 10 min
        status = "fresh"
    elif age < 900:     # 10-15 min
        status = "stale"
    else:               # > 15 min
        status = "dead"
    return {
        "exists": True,
        "age_seconds": round(age, 0),
        "last_beat_iso": datetime.fromtimestamp(
            HEARTBEAT_FILE.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "status": status,
    }


def _market_status() -> dict:
    """Is NSE market open right now?"""
    now_ist = datetime.now(IST)
    weekday = now_ist.weekday()
    open_  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    is_open = weekday < 5 and open_ <= now_ist <= close_
    return {
        "is_open": is_open,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "weekday": now_ist.strftime("%A"),
    }


_LOG_TICK_RE = re.compile(
    r"selected (\d+) symbols", re.IGNORECASE
)
_LOG_LATENCY_RE = re.compile(r"Signal latency ([\d.]+)s")
_LOG_OPEN_RE = re.compile(r"OPEN\s+(\S+)")
_LOG_CLOSE_RE = re.compile(r"CLOSE\s+(\S+).+reason=(\w+)")


def _log_summary(path: Path, tail_lines: int = 500) -> dict:
    """Parse the tail of the engine log for today's pipeline stats + recent errors."""
    out = {
        "path": str(path),
        "exists": path.exists(),
        "size_kb": 0,
        "today_opens": 0,
        "today_closes": 0,
        "today_sl_hits": 0,
        "today_target_hits": 0,
        "today_force_closes": 0,
        "latency_samples": [],
        "errors_recent": [],
        "warnings_recent": [],
        "universe_size": None,
    }
    if not path.exists():
        return out

    out["size_kb"] = round(path.stat().st_size / 1024, 1)

    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        # Read just the tail — full log may be huge
        with path.open("rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 256 * 1024))  # last 256 KB
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return out

    lines = data.splitlines()[-tail_lines:]

    errs, warns, lats = [], [], []
    for line in lines:
        if "OPEN " in line and _LOG_OPEN_RE.search(line):
            out["today_opens"] += 1
        if "CLOSE " in line:
            m = _LOG_CLOSE_RE.search(line)
            if m:
                out["today_closes"] += 1
                reason = m.group(2)
                if reason == "stop_loss":
                    out["today_sl_hits"] += 1
                elif reason == "target":
                    out["today_target_hits"] += 1
                elif "force" in reason:
                    out["today_force_closes"] += 1
        m = _LOG_LATENCY_RE.search(line)
        if m:
            lats.append(float(m.group(1)))
        m = _LOG_TICK_RE.search(line)
        if m:
            out["universe_size"] = int(m.group(1))
        # Last few errors/warnings (keep most recent)
        if "ERROR" in line:
            errs.append(line[-300:])
        elif "WARNING" in line and "Signal latency" not in line:
            warns.append(line[-300:])

    out["errors_recent"]   = errs[-10:]
    out["warnings_recent"] = warns[-10:]
    if lats:
        lats.sort()
        out["latency_samples"] = {
            "n": len(lats),
            "avg_sec":   round(sum(lats) / len(lats), 2),
            "max_sec":   round(lats[-1], 2),
            "p95_sec":   round(lats[int(len(lats) * 0.95)], 2),
            "over_1s":   sum(1 for x in lats if x > 1.0),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/full")
def health_full():
    """Everything the Health tab needs in one shot."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market":    _market_status(),
        "heartbeat": _heartbeat(),
        "vps":       _vps_vitals(),
        "services": {
            "intraday":  _systemd_state("nse-intraday.service"),
            "dashboard": _systemd_state("nse-dashboard.service"),
            "timer":     _systemd_state("nse-intraday.timer"),
        },
        "log": _log_summary(INTRADAY_LOG),
    }
