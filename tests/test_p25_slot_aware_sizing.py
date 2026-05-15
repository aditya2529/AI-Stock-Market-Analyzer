"""Regression test for P25 — slot-aware position sizing.

The original P1 proposal included a ``remaining_slots`` term that was
dropped from the shipped fix (df135b2). Without it, a day of 5 close-SL
signals can deploy ~95% of NSE cash with no headroom for the next bar.

The fix re-adds slot accounting:

    remaining_slots = max(1, max_positions - open_count_in_same_market)
    max_capital_per_trade = (portfolio_value / remaining_slots) * 0.80

So with no positions open and max_positions=5:
    cap = portfolio_value / 5 * 0.80 = 16% of per-market equity.
With 4 positions open and 1 remaining slot:
    cap = remaining_cash / 1 * 0.80 = 80% of remaining.

These tests monkey-patch ``paper_trading.portfolio.get_open_positions``
so they never touch the live SQLite state.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from paper_trading import executor, portfolio


def _empty_positions():
    return pd.DataFrame(columns=["symbol", "entry_price", "shares", "stop_loss", "target"])


def _positions_for(symbols, entry_price=100.0, shares=10):
    return pd.DataFrame([
        {"symbol": s, "entry_price": entry_price, "shares": shares,
         "stop_loss": entry_price * 0.99, "target": entry_price * 1.02}
        for s in symbols
    ])


def test_position_size_accepts_symbol_and_max_positions():
    """The new signature must accept symbol= and max_positions= kwargs."""
    import inspect
    sig = inspect.signature(executor._position_size)
    assert "symbol" in sig.parameters, (
        "P25 missing: _position_size must accept symbol= for slot-aware sizing"
    )
    assert "max_positions" in sig.parameters, (
        "P25 missing: _position_size must accept max_positions= for slot-aware sizing"
    )


def test_no_positions_open_caps_at_one_fifth_with_buffer(monkeypatch):
    """0 open NSE positions, max_positions=5 → cap = portfolio/5 * 0.80 = 16%."""
    monkeypatch.setattr(portfolio, "get_open_positions", _empty_positions)
    # Wide SL so risk-budget gate doesn't bind — cap gate must decide.
    # entry=100, SL=1 → risk_per_share=99 → risk_amount=1000 → shares_by_risk=10
    # ...too narrow. Use entry=100, SL=99 → risk_per_share=1 → shares_by_risk=1000.
    # cap = 100_000 / 5 * 0.80 = 16_000 → 160 shares at entry=100. min(1000,160)=160.
    shares = executor._position_size(
        entry_price=100.0, stop_loss=99.0, portfolio_value=100_000.0,
        symbol="RELIANCE.NS", max_positions=5,
    )
    assert shares == 160, f"expected 160 (16% of 100k at ₹100), got {shares}"


def test_fifth_slot_caps_at_80pct_of_remaining(monkeypatch):
    """4 NSE positions open, 1 slot remaining → cap = remaining * 0.80."""
    monkeypatch.setattr(
        portfolio, "get_open_positions",
        lambda: _positions_for(["A.NS", "B.NS", "C.NS", "D.NS"]),
    )
    # remaining_slots = 5 - 4 = 1
    # remaining_cash = 50_000 → cap = 50_000 * 0.80 = 40_000 → 400 shares at entry=100
    shares = executor._position_size(
        entry_price=100.0, stop_loss=99.0, portfolio_value=50_000.0,
        symbol="E.NS", max_positions=5,
    )
    assert shares == 400, f"expected 400 (80% of 50k at ₹100), got {shares}"


def test_nyse_count_does_not_steal_nse_slots(monkeypatch):
    """Open NYSE positions must not consume NSE slot budget."""
    monkeypatch.setattr(
        portfolio, "get_open_positions",
        lambda: _positions_for(["AAPL", "MSFT", "GOOG", "META"]),  # all NYSE
    )
    # No NSE positions → remaining_slots = 5 → cap = 100k/5*0.80 = 16k → 160 shares
    shares = executor._position_size(
        entry_price=100.0, stop_loss=99.0, portfolio_value=100_000.0,
        symbol="RELIANCE.NS", max_positions=5,
    )
    assert shares == 160, (
        f"expected 160 (NYSE positions must not occupy NSE slots), got {shares}"
    )


def test_five_back_to_back_buys_leave_buffer(monkeypatch):
    """Simulating 5 cap-bound BUYs at the slot cap must not drain NSE cash to zero.

    Acceptance gate from AUDIT_ROUND_2_BRIEF: after the 5th try_open at the
    cap, nse_cash must be ≥ 5% of nse_initial_cash. Pure sizing math here
    (no DB writes): we model the cash drawdown per trade using the slot
    cap formula and assert ≥5% headroom remains.
    """
    nse_initial = 500_000.0
    open_syms = []

    def fake_positions():
        return _positions_for(open_syms) if open_syms else _empty_positions()

    monkeypatch.setattr(portfolio, "get_open_positions", fake_positions)

    cash = nse_initial
    entry = 100.0  # cheap symbol so cap binds, not risk-budget
    for i in range(5):
        shares = executor._position_size(
            entry_price=entry, stop_loss=entry * 0.99,
            portfolio_value=cash, symbol=f"S{i}.NS", max_positions=5,
        )
        # Trade deploys shares * entry of cash; ignore fees for this math check.
        cash -= shares * entry
        open_syms.append(f"S{i}.NS")

    assert cash >= 0.05 * nse_initial, (
        f"After 5 cap-bound BUYs, NSE cash dropped to {cash:.2f} "
        f"(<5% of {nse_initial}). Slot-aware sizing failed to reserve buffer."
    )
