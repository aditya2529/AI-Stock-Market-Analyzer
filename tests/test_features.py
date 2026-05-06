"""Unit tests for features/engineer.py — one test per indicator."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest
from features.engineer import (
    compute_rsi, compute_macd, compute_bollinger, compute_atr,
    compute_vwap, compute_obv, compute_adx,
    compute_rolling_returns, compute_rolling_volatility,
    compute_time_features, engineer_features,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_df():
    """200 bars of synthetic price data with realistic properties."""
    np.random.seed(42)
    n = 200
    close = 1000 + np.cumsum(np.random.randn(n) * 5)
    high = close + np.abs(np.random.randn(n) * 3)
    low = close - np.abs(np.random.randn(n) * 3)
    volume = np.random.randint(100_000, 1_000_000, size=n)
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": close - 1, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


# ── Tests ────────────────────────────────────────────────────────────────────────

class TestRSI:
    def test_range(self, sample_df):
        rsi = compute_rsi(sample_df["close"])
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_length_preserved(self, sample_df):
        rsi = compute_rsi(sample_df["close"])
        assert len(rsi) == len(sample_df)

    def test_no_lookahead(self, sample_df):
        # Changing future rows should not affect past RSI values
        rsi_full = compute_rsi(sample_df["close"])
        truncated = sample_df.iloc[:100].copy()
        rsi_trunc = compute_rsi(truncated["close"])
        # Values up to index 99 should match
        pd.testing.assert_series_equal(rsi_full.iloc[:100].reset_index(drop=True),
                                        rsi_trunc.reset_index(drop=True), check_names=False)


class TestMACD:
    def test_returns_three_series(self, sample_df):
        macd, signal, hist = compute_macd(sample_df["close"])
        assert len(macd) == len(sample_df)
        assert len(signal) == len(sample_df)
        assert len(hist) == len(sample_df)

    def test_hist_is_difference(self, sample_df):
        macd, signal, hist = compute_macd(sample_df["close"])
        diff = (macd - signal - hist).dropna()
        assert (diff.abs() < 1e-9).all()


class TestBollingerBands:
    def test_upper_above_lower(self, sample_df):
        upper, lower, mid = compute_bollinger(sample_df["close"])
        valid = ~(upper.isna() | lower.isna())
        assert (upper[valid] >= lower[valid]).all()

    def test_mid_is_sma(self, sample_df):
        _, _, mid = compute_bollinger(sample_df["close"], period=20)
        sma = sample_df["close"].rolling(20).mean()
        pd.testing.assert_series_equal(mid.dropna(), sma.dropna(), check_names=False, atol=1e-9)


class TestATR:
    def test_positive(self, sample_df):
        atr = compute_atr(sample_df)
        assert (atr.dropna() > 0).all()

    def test_length_preserved(self, sample_df):
        assert len(compute_atr(sample_df)) == len(sample_df)


class TestVWAP:
    def test_between_low_and_high(self, sample_df):
        vwap = compute_vwap(sample_df)
        assert (vwap >= sample_df["low"].cummin()).all()
        assert (vwap <= sample_df["high"].cummax()).all()

    def test_no_nan(self, sample_df):
        assert not compute_vwap(sample_df).isna().any()


class TestOBV:
    def test_monotonic_on_rising_prices(self):
        n = 50
        close = pd.Series(np.linspace(100, 200, n))
        volume = pd.Series(np.ones(n) * 1000)
        df = pd.DataFrame({"close": close, "volume": volume,
                           "high": close + 1, "low": close - 1, "open": close})
        obv = compute_obv(df)
        assert obv.iloc[-1] > obv.iloc[0]


class TestADX:
    def test_range(self, sample_df):
        adx = compute_adx(sample_df)
        valid = adx.dropna()
        assert (valid >= 0).all()

    def test_length_preserved(self, sample_df):
        assert len(compute_adx(sample_df)) == len(sample_df)


class TestRollingReturns:
    def test_keys(self, sample_df):
        result = compute_rolling_returns(sample_df["close"])
        assert set(result.keys()) == {"return_5", "return_10", "return_20"}

    def test_return_5_manual(self, sample_df):
        expected = sample_df["close"].pct_change(5)
        pd.testing.assert_series_equal(compute_rolling_returns(sample_df["close"])["return_5"],
                                        expected, check_names=False)


class TestRollingVolatility:
    def test_keys(self, sample_df):
        result = compute_rolling_volatility(sample_df["close"])
        assert set(result.keys()) == {"volatility_10", "volatility_20"}

    def test_positive(self, sample_df):
        for series in compute_rolling_volatility(sample_df["close"]).values():
            assert (series.dropna() >= 0).all()


class TestTimeFeatures:
    def test_keys(self, sample_df):
        result = compute_time_features(sample_df.index)
        assert set(result.keys()) == {"hour_of_day", "day_of_week", "minutes_to_close"}

    def test_day_of_week_range(self, sample_df):
        dow = compute_time_features(sample_df.index)["day_of_week"]
        assert int(dow.min()) >= 0 and int(dow.max()) <= 6


class TestEngineerFeatures:
    def test_all_columns_present(self, sample_df):
        out = engineer_features(sample_df)
        expected_cols = {
            "rsi", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_lower", "bb_mid",
            "atr", "vwap", "obv", "adx",
            "return_5", "return_10", "return_20",
            "volatility_10", "volatility_20",
            "hour_of_day", "day_of_week", "minutes_to_close",
        }
        assert expected_cols.issubset(set(out.columns))

    def test_no_nan_in_features(self, sample_df):
        out = engineer_features(sample_df)
        feature_cols = [
            "rsi", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_lower", "bb_mid",
            "atr", "vwap", "obv", "adx",
            "return_5", "return_10", "return_20",
            "volatility_10", "volatility_20",
        ]
        assert not out[feature_cols].isna().any().any()

    def test_fewer_rows_than_input(self, sample_df):
        out = engineer_features(sample_df)
        assert len(out) < len(sample_df)  # warm-up rows dropped
