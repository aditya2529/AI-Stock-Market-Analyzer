"""Regression tests for P28 — daily safety gates in _process_symbol.

Three new guards fire BEFORE the BUY-open branch and return early with
explicit action codes the tick summary can count:

1. ``exposure_capped``    — total NSE open equity exceeds 80% of
                            nse_initial_cash (TOTAL_EXPOSURE_CAP).
2. ``daily_loss_halt``    — cumulative same-day net_pnl in paper_trades
                            is below -3% of nse_initial_cash
                            (DAILY_LOSS_LIMIT).
3. ``daily_count_capped`` — same-day trade count (closed + open)
                            reaches DAILY_TRADE_CAP (8).

Each test sets up the precondition in a temp paper DB, then calls
``intraday.engine._process_symbol`` with a stub ensemble that emits a
BUY signal, and asserts the expected action code is returned and no
new position is opened.

The tests monkeypatch ``data.database.DB_PATH`` to a temp file so they
never touch the live ``market_data.db``.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
import pandas as pd
import pytest


# ── Test fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp DB and seed paper tables with NSE allocation.

    nse_initial_cash = 500_000 so the brief's thresholds (₹15k loss,
    ₹400k exposure) hit at the documented percentages.
    """
    db_file = tmp_path / "test_p28.db"
    import data.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
    from paper_trading.portfolio import init_paper_tables, reset_portfolio
    init_paper_tables()
    reset_portfolio(initial_cash=1_000_000.0,
                    nse_allocation=500_000.0,
                    nyse_allocation=500_000.0)
    yield db_file


class _StubEnsemble:
    """Returns a BUY signal at high confidence + a trending-up regime
    so the existing regime/confidence gates pass and execution reaches
    the BUY-open branch where the P28 guards live."""

    def predict_with_confidence(self, featured):
        return pd.DataFrame([{
            "signal": "BUY", "regime": "TRENDING_UP", "confidence": 0.9,
        }], index=featured.index[-1:])


def _patch_feature_pipeline(monkeypatch):
    """Stub _fetch_intraday + engineer_features so _process_symbol reaches
    the gate logic without needing real DB OHLCV or macro context."""
    import intraday.engine as eng

    # Minimal df with 'close' + 'atr' so the BUY-open branch can compute
    # stop_loss/target.
    idx = pd.date_range("2026-05-18 10:00", periods=40, freq="5min")
    fake_df = pd.DataFrame({
        "open":  [100.0] * 40,
        "high":  [101.0] * 40,
        "low":   [99.0] * 40,
        "close": [100.0] * 40,
        "volume": [1000] * 40,
    }, index=idx)
    fake_featured = fake_df.copy()
    fake_featured["atr"] = 1.0

    monkeypatch.setattr(eng, "_fetch_intraday", lambda symbol: fake_df)
    monkeypatch.setattr("features.engineer.engineer_features",
                        lambda df: fake_featured)


def _seed_open_positions(symbols_with_values):
    """Insert open positions directly via open_position. Each entry is
    (symbol, entry_price, shares) — equity = entry_price * shares."""
    from paper_trading.portfolio import open_position
    for sym, price, shares in symbols_with_values:
        open_position(
            symbol=sym, entry_price=price, shares=shares,
            stop_loss=price * 0.95, target=price * 1.10,
            confidence=0.9, regime="TRENDING_UP",
        )


def _seed_closed_trades(rows):
    """Insert rows directly into paper_trades. Each row is
    (symbol, entry_price, exit_price, shares, net_pnl)."""
    import sqlite3
    from data.database import DB_PATH
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        now = datetime.utcnow().isoformat()
        for sym, entry, exit_p, shares, net_pnl in rows:
            conn.execute(
                "INSERT INTO paper_trades (symbol, entry_time, exit_time, "
                "entry_price, exit_price, shares, gross_pnl, net_pnl, "
                "return_pct, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sym, now, now, entry, exit_p, shares,
                 (exit_p - entry) * shares, net_pnl,
                 net_pnl / (entry * shares), "stop_loss"),
            )
        conn.commit()
    finally:
        conn.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_exposure_cap_blocks_when_over_80pct(tmp_db, monkeypatch):
    """4 NSE positions summing to > 80% of nse_initial (₹500k) → 5th BUY
    must return action=exposure_capped, no new position opened."""
    _patch_feature_pipeline(monkeypatch)
    # 4 positions: each ₹120k equity → total ₹480k > 0.80 * 500k = 400k
    _seed_open_positions([
        ("PAGEIND.NS", 1200.0, 100),
        ("MRF.NS",      600.0, 200),
        ("BAJFIN.NS",   800.0, 150),
        ("EICHERMOT.NS", 400.0, 300),
    ])
    from paper_trading.portfolio import get_open_positions
    assert len(get_open_positions()) == 4

    from intraday.engine import _process_symbol
    result = _process_symbol("RELIANCE.NS", _StubEnsemble(),
                             portfolio_value=500_000.0)
    assert result is not None and result.get("_action") == "exposure_capped"
    # No new position opened
    assert len(get_open_positions()) == 4


def test_daily_loss_halt_blocks_after_3pct_loss(tmp_db, monkeypatch):
    """Cumulative same-day net_pnl < -3% of nse_initial (₹500k → -₹15k)
    → next BUY must return action=daily_loss_halt."""
    _patch_feature_pipeline(monkeypatch)
    # Four ₹4k losses → -₹16k cumulative (< -₹15k threshold)
    _seed_closed_trades([
        ("A.NS", 100.0,  96.0, 1000, -4000.0),
        ("B.NS", 100.0,  96.0, 1000, -4000.0),
        ("C.NS", 100.0,  96.0, 1000, -4000.0),
        ("D.NS", 100.0,  96.0, 1000, -4000.0),
    ])
    from paper_trading.portfolio import get_open_positions
    assert get_open_positions().empty

    from intraday.engine import _process_symbol
    result = _process_symbol("RELIANCE.NS", _StubEnsemble(),
                             portfolio_value=500_000.0)
    assert result is not None and result.get("_action") == "daily_loss_halt"
    assert get_open_positions().empty


def test_daily_trade_cap_blocks_after_8(tmp_db, monkeypatch):
    """8 same-day trades counted in (closed + open) → next BUY must return
    action=daily_count_capped."""
    _patch_feature_pipeline(monkeypatch)
    # 8 small closed trades, all with TINY net_pnl so daily_loss gate
    # does NOT fire first (we want to isolate the trade-count gate).
    _seed_closed_trades([
        (f"T{i}.NS", 100.0, 100.5, 10, 5.0) for i in range(8)
    ])
    from intraday.engine import _process_symbol
    result = _process_symbol("RELIANCE.NS", _StubEnsemble(),
                             portfolio_value=500_000.0)
    assert result is not None and result.get("_action") == "daily_count_capped"
