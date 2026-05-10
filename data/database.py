from __future__ import annotations
import sqlite3
import pandas as pd
from pathlib import Path
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT NOT NULL,
                market    TEXT NOT NULL DEFAULT 'NSE',
                resolution TEXT NOT NULL DEFAULT '1d',
                time      TEXT NOT NULL,
                open      REAL,
                high      REAL,
                low       REAL,
                close     REAL,
                volume    INTEGER,
                UNIQUE(symbol, resolution, time)
            );
            CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time
                ON ohlcv (symbol, resolution, time DESC);
        """)


def upsert_ohlcv(df: pd.DataFrame, symbol: str, market: str = "NSE", resolution: str = "1d"):
    """Insert or replace OHLCV rows. df must have columns: time, open, high, low, close, volume."""
    rows = [
        (symbol, market, resolution, str(row.time), row.open, row.high, row.low, row.close, int(row.volume))
        for row in df.itertuples()
    ]
    with get_connection() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO ohlcv
               (symbol, market, resolution, time, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def load_ohlcv(symbol: str, resolution: str = "1d", limit: int = None) -> pd.DataFrame:
    sql = """
        SELECT time, open, high, low, close, volume
        FROM ohlcv
        WHERE symbol = ? AND resolution = ?
        ORDER BY time ASC
    """
    params = [symbol, resolution]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection() as conn:
        df = pd.read_sql_query(sql, conn, params=params, parse_dates=["time"])
    df.set_index("time", inplace=True)
    return df


def list_symbols() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol").fetchall()
    return [r[0] for r in rows]
