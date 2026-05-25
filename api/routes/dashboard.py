"""P46 — Dashboard observability endpoints.

All endpoints are additive read-only. They never mutate state, never touch
the engine, never touch model/signals/features code. They parse logs,
read paper_config, and re-fetch OHLCV bars to derive trader-grade metrics
that the engine doesn't already expose.

Endpoints:
- GET /api/dashboard/cooldowns           — active SL cooldown list (P30 state)
- GET /api/dashboard/conf_distribution   — last-tick confidence histogram
- GET /api/dashboard/regime_distribution — universe-wide regime counts
- GET /api/dashboard/sector_exposure     — sector breakdown of open + today's trades
- GET /api/dashboard/signal_latency      — recent latency stats from engine log
- GET /api/dashboard/mae_mfe             — Max Adverse / Favorable Excursion per open position
- GET /api/dashboard/daily_loss_budget   — today's P&L vs 3% daily cap
- GET /api/dashboard/force_close         — countdown to 15:15 IST force-close
"""
from __future__ import annotations
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

IST = timezone(timedelta(hours=5, minutes=30))
LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"


# ─────────────────────────────────────────────────────────────────────────────
# Static sector map — covers the daily-eligible universe + any historically
# traded symbol. Falls back to "Other" for anything unknown. Maintained by ops.
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    # IT
    "TCS.NS": "IT", "INFY.NS": "IT", "WIPRO.NS": "IT", "HCLTECH.NS": "IT",
    "TECHM.NS": "IT", "LTIM.NS": "IT", "KPITTECH.NS": "IT", "COFORGE.NS": "IT",
    "MPHASIS.NS": "IT", "PERSISTENT.NS": "IT",
    # Banking & Finance
    "HDFCBANK.NS": "Banking", "ICICIBANK.NS": "Banking", "KOTAKBANK.NS": "Banking",
    "AXISBANK.NS": "Banking", "SBIN.NS": "Banking", "INDUSINDBK.NS": "Banking",
    "BANKBARODA.NS": "Banking", "PNB.NS": "Banking", "FEDERALBNK.NS": "Banking",
    "IDFCFIRSTB.NS": "Banking", "IDBI.NS": "Banking", "RBLBANK.NS": "Banking",
    "UJJIVANSFB.NS": "Banking", "BANDHANBNK.NS": "Banking", "AUBANK.NS": "Banking",
    "BAJFINANCE.NS": "Banking", "BAJAJFINSV.NS": "Banking", "CHOLAFIN.NS": "Banking",
    "HDFCAMC.NS": "Banking", "ICICIPRULI.NS": "Banking", "LICI.NS": "Banking",
    "MFSL.NS": "Banking", "SBILIFE.NS": "Banking", "HDFCLIFE.NS": "Banking",
    "POLICYBZR.NS": "Banking", "PAYTM.NS": "Banking",
    # Energy & Oil
    "RELIANCE.NS": "Energy", "ONGC.NS": "Energy", "BPCL.NS": "Energy",
    "IOC.NS": "Energy", "HINDPETRO.NS": "Energy", "GAIL.NS": "Energy",
    "PETRONET.NS": "Energy", "IGL.NS": "Energy", "GSPL.NS": "Energy",
    # Auto
    "MARUTI.NS": "Auto", "M&M.NS": "Auto", "BAJAJ-AUTO.NS": "Auto",
    "TATAMOTORS.NS": "Auto", "EICHERMOT.NS": "Auto", "HEROMOTOCO.NS": "Auto",
    "TVSMOTOR.NS": "Auto", "ASHOKLEY.NS": "Auto", "MOTHERSON.NS": "Auto",
    "BOSCHLTD.NS": "Auto", "MRF.NS": "Auto", "BALKRISIND.NS": "Auto",
    # Pharma
    "SUNPHARMA.NS": "Pharma", "DRREDDY.NS": "Pharma", "CIPLA.NS": "Pharma",
    "DIVISLAB.NS": "Pharma", "AUROPHARMA.NS": "Pharma", "GLENMARK.NS": "Pharma",
    "BIOCON.NS": "Pharma", "TORNTPHARM.NS": "Pharma", "LUPIN.NS": "Pharma",
    "ZYDUSLIFE.NS": "Pharma", "MANKIND.NS": "Pharma", "GRANULES.NS": "Pharma",
    "LALPATHLAB.NS": "Pharma", "ALKEM.NS": "Pharma", "APOLLOHOSP.NS": "Pharma",
    # FMCG / Consumer
    "HINDUNILVR.NS": "FMCG", "NESTLEIND.NS": "FMCG", "BRITANNIA.NS": "FMCG",
    "ITC.NS": "FMCG", "TATACONSUM.NS": "FMCG", "DABUR.NS": "FMCG",
    "MARICO.NS": "FMCG", "GODREJCP.NS": "FMCG", "COLPAL.NS": "FMCG",
    "JUBLFOOD.NS": "FMCG", "GODREJIND.NS": "FMCG", "NYKAA.NS": "FMCG",
    "TRENT.NS": "FMCG", "TITAN.NS": "FMCG", "ASIANPAINT.NS": "FMCG",
    "BERGEPAINT.NS": "FMCG", "PIDILITIND.NS": "FMCG", "DMART.NS": "FMCG",
    # Metals & Mining
    "TATASTEEL.NS": "Metals", "HINDALCO.NS": "Metals", "JSWSTEEL.NS": "Metals",
    "SAIL.NS": "Metals", "VEDL.NS": "Metals", "NMDC.NS": "Metals",
    "COALINDIA.NS": "Metals", "JINDALSTEL.NS": "Metals", "WELCORP.NS": "Metals",
    # Telecom & Infra & Cement
    "BHARTIARTL.NS": "Telecom", "IDEA.NS": "Telecom", "TATACOMM.NS": "Telecom",
    "INDIAMART.NS": "Telecom", "NAUKRI.NS": "Telecom", "ZEEL.NS": "Telecom",
    "LT.NS": "Infra", "ULTRACEMCO.NS": "Infra", "GRASIM.NS": "Infra",
    "AMBUJACEM.NS": "Infra", "ACC.NS": "Infra", "SHREECEM.NS": "Infra",
    "DLF.NS": "Infra", "GODREJPROP.NS": "Infra", "OBEROIRLTY.NS": "Infra",
    "IBREALEST.NS": "Infra", "ASTRAL.NS": "Infra", "POLYCAB.NS": "Infra",
    "DIXON.NS": "Infra", "BHEL.NS": "Infra", "SIEMENS.NS": "Infra",
    "HAVELLS.NS": "Infra", "VOLTAS.NS": "Infra", "ABB.NS": "Infra",
    "CUMMINSIND.NS": "Infra", "PEL.NS": "Infra",
    # Power / Utilities
    "NTPC.NS": "Power", "POWERGRID.NS": "Power", "TATAPOWER.NS": "Power",
    "ADANIPOWER.NS": "Power", "ADANIGREEN.NS": "Power", "ADANIENT.NS": "Power",
    "ADANIPORTS.NS": "Power", "ADANIENSOL.NS": "Power", "JSWENERGY.NS": "Power",
    "CESC.NS": "Power", "TORNTPOWER.NS": "Power", "RAIN.NS": "Power",
    # Chemicals
    "UPL.NS": "Chemicals", "PIIND.NS": "Chemicals", "SRF.NS": "Chemicals",
    "AARTIIND.NS": "Chemicals", "DEEPAKNTR.NS": "Chemicals", "TATACHEM.NS": "Chemicals",
    "CHAMBLFERT.NS": "Chemicals", "COROMANDEL.NS": "Chemicals",
    # Misc
    "TTKPRESTIG.NS": "FMCG",
}


def _sector_of(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, "Other")


# ─────────────────────────────────────────────────────────────────────────────
# Log parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _today_log_path() -> Path:
    today_ist = datetime.now(IST).date()
    return LOGS_DIR / f"intraday_{today_ist.strftime('%Y%m%d')}.log"


def _read_tail(path: Path, max_bytes: int = 256 * 1024) -> list[str]:
    """Read the tail of a file as a list of lines. Empty list on any failure."""
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()
    except Exception:
        return []


# Match engine log lines that name a symbol + confidence:
#   INFO | IGL.NS: conf 0.56 below floor 0.60 — skipping SELL
#   INFO | CESC.NS: conf 0.34 below floor 0.60 — skipping BUY
_CONF_LINE_RE = re.compile(
    r"^INFO\s*\|\s*(?P<sym>[A-Z0-9&\-]+\.NS):\s*conf\s+(?P<conf>[\d.]+)",
    re.IGNORECASE,
)

# OPEN/CLOSE actions:
#   OPEN MFSL.NS @1676.58, SL=1671.44, TGT=1686.86, conf=0.62, regime=SIDEWAYS
_OPEN_RE = re.compile(
    r"OPEN\s+(?P<sym>\S+)\s+@(?P<price>[\d.]+).*?conf=(?P<conf>[\d.]+).*?regime=(?P<regime>\w+)",
    re.IGNORECASE,
)

# Universe scan: "selected 50 symbols. Top 5: ['...', '...']"
_UNIVERSE_RE = re.compile(r"selected\s+(\d+)\s+symbols", re.IGNORECASE)

# Tick summary
_TICK_RE = re.compile(
    r"Tick summary: processed=(?P<n>\d+).*?conf_blocked=(?P<conf_blocked>\d+).*?"
    r"cooldown=(?P<cd>\d+).*?opened=(?P<opened>\d+).*?closed=(?P<closed>\d+)",
    re.IGNORECASE,
)

# Signal latency warning
_LATENCY_RE = re.compile(r"Signal latency ([\d.]+)s")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/cooldowns")
def get_cooldowns():
    """Active SL cooldown list — symbols blocked for re-entry today after an SL hit.

    Reads from paper_config (key = sl_cooldown_YYYY-MM-DD), exactly the same
    source the engine uses on restart. Output is sorted alphabetically.
    """
    try:
        from paper_trading.portfolio import init_paper_tables, get_config
        init_paper_tables()
        today_ist = datetime.now(IST).date().isoformat()
        raw = get_config(f"sl_cooldown_{today_ist}", "") or ""
        symbols = sorted(s for s in raw.split(",") if s)
        return {
            "date": today_ist,
            "n_cooled": len(symbols),
            "symbols": symbols,
            "note": "Symbols blocked for re-entry today after stop-loss hit. Resets at session start tomorrow.",
        }
    except Exception as e:
        return {"date": None, "n_cooled": 0, "symbols": [], "error": str(e)}


@router.get("/conf_distribution")
def get_conf_distribution():
    """Confidence-score histogram from the most recent tick.

    Parses today's engine log for the last block of per-symbol conf lines
    and buckets them into 0.0–0.1, 0.1–0.2, … 0.9–1.0. Also returns the
    count above the 0.60 floor (the gate that decides if a trade fires).
    """
    lines = _read_tail(_today_log_path())
    if not lines:
        return {"buckets": [], "above_floor": 0, "total_seen": 0, "floor": 0.60,
                "note": "Engine log empty or not yet started today."}

    # Find the most recent tick boundary, then collect conf lines from that tick
    last_tick_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "Tick summary" in lines[i]:
            last_tick_idx = i
            break

    # Look backwards from last tick to find the previous tick boundary
    prev_tick_idx = -1
    if last_tick_idx is not None:
        for i in range(last_tick_idx - 1, -1, -1):
            if "Tick summary" in lines[i]:
                prev_tick_idx = i
                break

    window = lines[(prev_tick_idx + 1):(last_tick_idx + 1)] if last_tick_idx else lines[-200:]

    confs: list[float] = []
    for line in window:
        m = _CONF_LINE_RE.search(line)
        if m:
            try:
                confs.append(float(m.group("conf")))
            except ValueError:
                pass

    # 10 buckets of width 0.1
    buckets = [{"range": f"{i/10:.1f}-{(i+1)/10:.1f}", "count": 0} for i in range(10)]
    for c in confs:
        idx = min(int(c * 10), 9)
        buckets[idx]["count"] += 1

    above_floor = sum(1 for c in confs if c >= 0.60)
    return {
        "buckets": buckets,
        "above_floor": above_floor,
        "total_seen": len(confs),
        "floor": 0.60,
        "note": "Conf scores logged this tick. Universe stocks with no log line "
                "passed silently (no signal worth noting).",
    }


@router.get("/regime_distribution")
def get_regime_distribution():
    """Universe-wide regime counts.

    Today's engine log doesn't currently emit per-tick regime breakdowns,
    so this endpoint derives an approximation from the most recent OPEN
    events and currently-open positions. If the engine starts logging
    full regime tick summaries (future P-item), this can be upgraded.
    """
    counts: Counter = Counter()
    try:
        from paper_trading.portfolio import init_paper_tables, get_open_positions
        init_paper_tables()
        positions = get_open_positions()
        if not positions.empty:
            for _, row in positions.iterrows():
                regime = row.get("regime") or "UNKNOWN"
                counts[regime] += 1
    except Exception:
        pass

    # Also scan today's log OPEN events for regime
    for line in _read_tail(_today_log_path()):
        m = _OPEN_RE.search(line)
        if m:
            counts[m.group("regime").upper()] += 1

    if not counts:
        return {"regimes": [], "total": 0,
                "note": "No regime data yet today (no OPEN events, no open positions)."}

    return {
        "regimes": [{"name": k, "count": v} for k, v in counts.most_common()],
        "total": sum(counts.values()),
        "note": "Counts from today's OPEN events + currently open positions. "
                "Not a live universe-wide snapshot — pending engine instrumentation.",
    }


@router.get("/sector_exposure")
def get_sector_exposure():
    """Sector breakdown of open positions + today's closed trades.

    Returns two views:
      - open: sector → list of symbols + notional value
      - today_closed: sector → net P&L sum
    Helps spot if losses are concentrated in one sector.
    """
    open_by_sector: dict[str, dict] = {}
    closed_by_sector: dict[str, dict] = {}

    try:
        from paper_trading.portfolio import init_paper_tables, get_open_positions, get_trade_history
        init_paper_tables()

        positions = get_open_positions()
        if not positions.empty:
            for _, row in positions.iterrows():
                sym = row["symbol"]
                sector = _sector_of(sym)
                notional = float(row["entry_price"]) * int(row["shares"])
                bucket = open_by_sector.setdefault(
                    sector, {"sector": sector, "symbols": [], "notional": 0.0, "n_positions": 0}
                )
                bucket["symbols"].append(sym)
                bucket["notional"] += notional
                bucket["n_positions"] += 1

        trades = get_trade_history()
        if not trades.empty:
            today_ist = datetime.now(IST).date().isoformat()
            today_trades = trades[trades["exit_time"].str.startswith(today_ist, na=False)]
            for _, row in today_trades.iterrows():
                sym = row["symbol"]
                sector = _sector_of(sym)
                pnl = float(row["net_pnl"])
                bucket = closed_by_sector.setdefault(
                    sector, {"sector": sector, "n_trades": 0, "wins": 0, "net_pnl": 0.0, "symbols": []}
                )
                bucket["n_trades"] += 1
                if pnl > 0:
                    bucket["wins"] += 1
                bucket["net_pnl"] += pnl
                if sym not in bucket["symbols"]:
                    bucket["symbols"].append(sym)
    except Exception as e:
        return {"open": [], "today_closed": [], "error": str(e)}

    # Round + sort
    for b in open_by_sector.values():
        b["notional"] = round(b["notional"], 2)
    for b in closed_by_sector.values():
        b["net_pnl"] = round(b["net_pnl"], 2)

    return {
        "open": sorted(open_by_sector.values(), key=lambda x: -x["notional"]),
        "today_closed": sorted(closed_by_sector.values(), key=lambda x: x["net_pnl"]),
    }


@router.get("/signal_latency")
def get_signal_latency():
    """Signal generation latency stats from today's engine log.

    The engine only logs latency lines when a signal exceeds the gate
    (GATE_SIGNAL_LATENCY_SEC = 1.0s). No log lines is the healthy case
    — return that explicitly so the dashboard can show a green "all
    signals under gate" indicator.
    """
    lines = _read_tail(_today_log_path())
    samples: list[float] = []
    for line in lines:
        m = _LATENCY_RE.search(line)
        if m:
            try:
                samples.append(float(m.group(1)))
            except ValueError:
                pass

    if not samples:
        return {
            "n_breaches": 0,
            "gate_sec": 1.0,
            "avg_sec": None,
            "p95_sec": None,
            "max_sec": None,
            "last_sec": None,
            "status": "all-under-gate",
            "note": "No signal latency warnings today. All signals generated under 1.0s gate.",
        }

    samples.sort()
    return {
        "n_breaches": len(samples),
        "gate_sec": 1.0,
        "avg_sec": round(sum(samples) / len(samples), 2),
        "p95_sec": round(samples[int(len(samples) * 0.95)], 2),
        "max_sec": round(samples[-1], 2),
        "last_sec": round(samples[-1], 2),
        "status": "breaches-detected",
        "note": f"{len(samples)} signal(s) exceeded the 1.0s gate today.",
    }


@router.get("/mae_mfe")
def get_mae_mfe():
    """Max Adverse / Favorable Excursion per open position.

    For each open position, fetches the symbol's 5-min bars since entry,
    finds the lowest low (MAE) and highest high (MFE), and returns both
    as % from entry price. Helps spot if SLs are too tight (high MAE %)
    or targets too far (high MFE % without target hit).
    """
    out = []
    try:
        from paper_trading.portfolio import init_paper_tables, get_open_positions
        from data.database import load_ohlcv
        init_paper_tables()
        positions = get_open_positions()
        if positions.empty:
            return {"positions": [], "note": "No open positions."}

        for _, row in positions.iterrows():
            sym = row["symbol"]
            entry_price = float(row["entry_price"])
            entry_time_str = str(row["entry_time"])
            # Parse entry_time (UTC ISO) and convert to a comparable timestamp
            try:
                entry_ts = datetime.fromisoformat(entry_time_str.replace("Z", ""))
            except ValueError:
                entry_ts = None

            mae_pct: Optional[float] = None
            mfe_pct: Optional[float] = None
            try:
                df = load_ohlcv(sym, resolution="5m")
                if df is not None and not df.empty and entry_ts is not None:
                    # Filter to bars at/after entry time
                    df = df.reset_index() if "time" not in df.columns else df
                    if "time" in df.columns:
                        import pandas as pd
                        df["time"] = pd.to_datetime(df["time"])
                        df = df[df["time"] >= entry_ts]
                    if not df.empty:
                        max_high = float(df["high"].max())
                        min_low = float(df["low"].min())
                        mfe_pct = round(((max_high - entry_price) / entry_price) * 100, 2)
                        mae_pct = round(((min_low - entry_price) / entry_price) * 100, 2)
            except Exception:
                pass

            out.append({
                "symbol": sym,
                "entry_price": entry_price,
                "stop_loss": float(row["stop_loss"]),
                "target": float(row["target"]),
                "mae_pct": mae_pct,
                "mfe_pct": mfe_pct,
                "sl_distance_pct": round(((float(row["stop_loss"]) - entry_price) / entry_price) * 100, 2),
                "tgt_distance_pct": round(((float(row["target"]) - entry_price) / entry_price) * 100, 2),
            })
        return {"positions": out, "note": "MAE/MFE since entry, from 5-min bars."}
    except Exception as e:
        return {"positions": out, "error": str(e)}


@router.get("/daily_loss_budget")
def get_daily_loss_budget():
    """Today's running P&L vs the 3% daily loss limit.

    The engine enforces a hard halt at -3% of peak portfolio. This endpoint
    surfaces how much of that budget has been spent so the trader can see
    risk-of-halt at a glance.
    """
    try:
        from paper_trading.portfolio import (
            init_paper_tables, get_config, get_trade_history,
        )
        from config import DAILY_LOSS_LIMIT_PCT
        init_paper_tables()

        peak = float(get_config("peak_value", "500000"))
        budget_total = peak * DAILY_LOSS_LIMIT_PCT  # ₹15,000 on a ₹500K peak

        trades = get_trade_history()
        today_pnl = 0.0
        n_today = 0
        if not trades.empty:
            today_ist = datetime.now(IST).date().isoformat()
            today_trades = trades[trades["exit_time"].str.startswith(today_ist, na=False)]
            n_today = len(today_trades)
            today_pnl = float(today_trades["net_pnl"].sum())

        spent = abs(min(today_pnl, 0.0))  # only count net losses against the budget
        remaining = max(0.0, budget_total - spent)
        pct_used = (spent / budget_total) if budget_total > 0 else 0.0

        return {
            "limit_pct": DAILY_LOSS_LIMIT_PCT,
            "budget_total": round(budget_total, 2),
            "spent": round(spent, 2),
            "remaining": round(remaining, 2),
            "pct_used": round(pct_used, 4),
            "today_pnl": round(today_pnl, 2),
            "n_today_trades": n_today,
            "halted": spent >= budget_total,
            "note": "Budget = 3% of peak portfolio value. Engine auto-halts at spent >= budget.",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/force_close")
def get_force_close():
    """Countdown to the daily force-close at 15:15 IST.

    Returns seconds remaining + a friendly string. Negative when past the
    cutoff. The frontend can render either as a live ticker — both are
    derived purely from IST clock + the configured force-close hour/minute.
    """
    try:
        from config import INTRADAY_FORCE_CLOSE_TIME
        hh, mm = INTRADAY_FORCE_CLOSE_TIME
    except Exception:
        hh, mm = 15, 15

    now_ist = datetime.now(IST)
    cutoff = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (cutoff - now_ist).total_seconds()
    is_past = delta < 0
    abs_delta = int(abs(delta))
    hours = abs_delta // 3600
    minutes = (abs_delta % 3600) // 60
    seconds = abs_delta % 60

    if is_past:
        label = f"force-closed {hours}h {minutes}m ago"
    elif now_ist.weekday() >= 5:
        label = "weekend — market closed"
    else:
        label = f"{hours}h {minutes:02d}m to 15:15 IST"

    return {
        "now_ist": now_ist.isoformat(),
        "force_close_at_ist": cutoff.isoformat(),
        "seconds_remaining": int(delta),
        "is_past": is_past,
        "is_weekend": now_ist.weekday() >= 5,
        "label": label,
    }
