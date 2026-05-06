"""Paper trading portfolio — SQLite-backed positions, trades, and equity snapshots."""
import sqlite3
from datetime import datetime
from typing import Optional
import pandas as pd
from data.database import get_connection


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_paper_tables():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                entry_time      TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                shares          INTEGER NOT NULL,
                stop_loss       REAL NOT NULL,
                target          REAL NOT NULL,
                confidence      REAL,
                regime          TEXT,
                UNIQUE(symbol)
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                entry_time      TEXT NOT NULL,
                exit_time       TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                exit_price      REAL NOT NULL,
                shares          INTEGER NOT NULL,
                gross_pnl       REAL NOT NULL,
                net_pnl         REAL NOT NULL,
                return_pct      REAL NOT NULL,
                exit_reason     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_portfolio_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                cash            REAL NOT NULL,
                open_equity     REAL NOT NULL,
                total_value     REAL NOT NULL,
                peak_value      REAL NOT NULL,
                drawdown_pct    REAL NOT NULL,
                n_open          INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );
        """)
        # Seed initial portfolio value if not set
        conn.execute(
            "INSERT OR IGNORE INTO paper_config (key, value) VALUES ('initial_cash', '100000')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_config (key, value) VALUES ('peak_value', '100000')"
        )


# ── Portfolio state ─────────────────────────────────────────────────────────────

def get_config(key: str, default=None) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM paper_config WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_config(key: str, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_config (key, value) VALUES (?, ?)",
            (key, str(value))
        )


def get_cash() -> float:
    return float(get_config("cash", get_config("initial_cash", "100000")))


def set_cash(amount: float):
    set_config("cash", amount)


def get_open_positions() -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM paper_positions ORDER BY entry_time", conn
        )
    return df


def get_position(symbol: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)).description]
    # Re-fetch with column names
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)
        ).fetchone()
    return dict(row) if row else None


def open_position(symbol: str, entry_price: float, shares: int,
                  stop_loss: float, target: float,
                  confidence: float = None, regime: str = None):
    cash = get_cash()
    cost = entry_price * shares
    if cost > cash:
        raise ValueError(f"Insufficient cash: need {cost:.2f}, have {cash:.2f}")
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO paper_positions
               (symbol, entry_time, entry_price, shares, stop_loss, target, confidence, regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, datetime.utcnow().isoformat(), entry_price, shares,
             stop_loss, target, confidence, regime)
        )
    set_cash(cash - cost)


def close_position(symbol: str, exit_price: float, exit_reason: str) -> dict:
    pos = get_position(symbol)
    if pos is None:
        raise ValueError(f"No open position for {symbol}")

    shares = pos["shares"]
    entry_price = pos["entry_price"]
    gross_proceeds = exit_price * shares
    entry_cost = entry_price * shares

    from config import BROKERAGE_PCT, SLIPPAGE_PCT
    entry_cost_with_fees = entry_cost * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)
    exit_proceeds_net = gross_proceeds * (1 - BROKERAGE_PCT - SLIPPAGE_PCT)

    gross_pnl = gross_proceeds - entry_cost
    net_pnl = exit_proceeds_net - entry_cost_with_fees
    return_pct = net_pnl / entry_cost_with_fees

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO paper_trades
               (symbol, entry_time, exit_time, entry_price, exit_price, shares,
                gross_pnl, net_pnl, return_pct, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, pos["entry_time"], datetime.utcnow().isoformat(),
             entry_price, exit_price, shares, gross_pnl, net_pnl, return_pct, exit_reason)
        )
        conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))

    cash = get_cash()
    set_cash(cash + exit_proceeds_net)
    return {"symbol": symbol, "net_pnl": net_pnl, "return_pct": return_pct, "exit_reason": exit_reason}


def compute_open_equity(prices: dict) -> float:
    """Mark open positions to market. prices = {symbol: current_price}."""
    positions = get_open_positions()
    if positions.empty:
        return 0.0
    total = 0.0
    for _, pos in positions.iterrows():
        price = prices.get(pos["symbol"], pos["entry_price"])
        total += price * pos["shares"]
    return total


def snapshot_portfolio(prices: dict):
    """Log current portfolio state."""
    cash = get_cash()
    open_eq = compute_open_equity(prices)
    total = cash + open_eq
    peak = float(get_config("peak_value", str(total)))
    if total > peak:
        peak = total
        set_config("peak_value", peak)
    drawdown = (peak - total) / peak if peak > 0 else 0.0
    n_open = len(get_open_positions())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO paper_portfolio_log
               (timestamp, cash, open_equity, total_value, peak_value, drawdown_pct, n_open)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), cash, open_eq, total, peak, drawdown, n_open)
        )
    return {"cash": cash, "open_equity": open_eq, "total_value": total,
            "peak_value": peak, "drawdown_pct": drawdown}


def get_trade_history() -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM paper_trades ORDER BY exit_time DESC", conn
        )
    return df


def get_portfolio_log() -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM paper_portfolio_log ORDER BY timestamp", conn
        )
    return df


def reset_portfolio(initial_cash: float = 100_000.0):
    """Wipe paper trading state and restart with fresh cash."""
    with get_connection() as conn:
        conn.executescript("""
            DELETE FROM paper_positions;
            DELETE FROM paper_trades;
            DELETE FROM paper_portfolio_log;
        """)
        conn.execute(
            "INSERT OR REPLACE INTO paper_config (key, value) VALUES ('initial_cash', ?)",
            (str(initial_cash),)
        )
        conn.execute(
            "INSERT OR REPLACE INTO paper_config (key, value) VALUES ('cash', ?)",
            (str(initial_cash),)
        )
        conn.execute(
            "INSERT OR REPLACE INTO paper_config (key, value) VALUES ('peak_value', ?)",
            (str(initial_cash),)
        )
