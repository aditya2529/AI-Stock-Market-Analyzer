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
    # P24: open a dedicated SQLite connection per call with
    # check_same_thread=False. The 8-worker ThreadPoolExecutor in
    # intraday/engine.py concurrently called pd.read_sql_query against
    # connections returned by get_connection() — and get_connection()
    # additionally executes ``PRAGMA journal_mode=WAL`` on every open,
    # which leaked C state across threads and triggered heap corruption
    # in pandas's _fetchall_as_list (see logs/faulthandler.log). A bare
    # sqlite3.connect with check_same_thread=False, no PRAGMA, and a
    # finally-close keeps each thread's read path fully isolated.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        df = pd.read_sql_query(sql, conn, params=params, parse_dates=["time"])
    finally:
        conn.close()
    df.set_index("time", inplace=True)
    return df


def list_symbols() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol").fetchall()
    return [r[0] for r in rows]


def list_tradeable_symbols(resolution: str = "1d") -> list[str]:
    """Return tradeable symbols only — excludes macro indices (^NSEI, ^INDIAVIX, etc.)
    and filters by resolution so macro-only rows don't bleed in."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM ohlcv WHERE resolution = ? ORDER BY symbol",
            (resolution,),
        ).fetchall()
    return [r[0] for r in rows if not r[0].startswith("^")]
