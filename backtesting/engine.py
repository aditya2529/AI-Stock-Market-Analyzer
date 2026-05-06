"""Walk-forward backtesting engine.

Flow:
    1. Split feature DataFrame into WF_FOLDS chronological folds.
    2. For each fold:  train on preceding data, test on fold.
    3. Simulate trades on test predictions (long-only, daily bars).
    4. Aggregate metrics across all folds.
"""
import logging
from dateutil.relativedelta import relativedelta
import pandas as pd
import numpy as np
from config import (
    WF_FOLDS, WF_FOLD_MONTHS, WF_STEP_MONTHS,
    BROKERAGE_PCT, SLIPPAGE_PCT, FEATURE_COLUMNS,
    LABEL_LOOKAHEAD, BUY_THRESHOLD, SELL_THRESHOLD,
)
from backtesting.metrics import compute_all

logger = logging.getLogger(__name__)

SIGNAL_MAP = {"BUY": 1, "HOLD": 0, "SELL": -1}


def make_labels(df: pd.DataFrame) -> pd.Series:
    """Create forward-return labels: BUY / SELL / HOLD."""
    fwd_return = df["close"].shift(-LABEL_LOOKAHEAD) / df["close"] - 1
    labels = pd.Series("HOLD", index=df.index)
    labels[fwd_return >= BUY_THRESHOLD] = "BUY"
    labels[fwd_return <= SELL_THRESHOLD] = "SELL"
    return labels


def _simulate_trades(df_test: pd.DataFrame, predictions: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """Simulate long-only trades from model predictions.

    Returns:
        trades  — DataFrame of completed trades with [entry, exit, pnl, return]
        equity  — equity curve Series (starting at 1.0)
    """
    portfolio = 1.0
    position = None  # {"entry_price": float, "entry_idx": int}
    trades = []
    equity = [portfolio]

    prices = df_test["close"].values
    signals = predictions.values

    for i, (price, signal) in enumerate(zip(prices, signals)):
        cost = price * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)

        if position is None and signal == "BUY":
            position = {"entry_price": cost, "entry_bar": i}
        elif position is not None and signal in ("SELL", "HOLD"):
            exit_price = price * (1 - BROKERAGE_PCT - SLIPPAGE_PCT)
            trade_return = exit_price / position["entry_price"] - 1
            pnl = portfolio * trade_return
            trades.append({"pnl": pnl, "return": trade_return,
                           "entry_bar": position["entry_bar"], "exit_bar": i})
            portfolio *= (1 + trade_return)
            position = None

        equity.append(portfolio)

    equity_series = pd.Series(equity, name="equity")
    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame(columns=["pnl", "return", "entry_bar", "exit_bar"])
    return trades_df, equity_series


def run_walk_forward(df: pd.DataFrame, model_cls, model_kwargs: dict = None) -> dict:
    """Run walk-forward backtest.

    Args:
        df:          Feature-engineered DataFrame with DatetimeIndex.
        model_cls:   A class with fit(X, y) and predict(X) → array of label strings.
        model_kwargs: Extra kwargs passed to model_cls constructor.

    Returns:
        dict with per-fold metrics and aggregated summary.
    """
    model_kwargs = model_kwargs or {}
    labels = make_labels(df)
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]

    dates = df.index
    fold_results = []

    # Build fold boundaries
    folds = []
    test_start = dates[int(len(dates) * 0.75)]  # first 75% always training — folds land in 2023-2025
    for _ in range(WF_FOLDS):
        test_end = test_start + relativedelta(months=WF_FOLD_MONTHS)
        if test_end > dates[-1]:
            break
        folds.append((test_start, test_end))
        test_start = test_start + relativedelta(months=WF_STEP_MONTHS)

    if not folds:
        raise ValueError("Not enough data to create walk-forward folds. Fetch more history.")

    all_trades = []
    all_equity = []

    for i, (ts, te) in enumerate(folds):
        train_mask = dates < ts
        test_mask = (dates >= ts) & (dates < te)

        X_train = df.loc[train_mask, feature_cols]
        y_train = labels.loc[train_mask]
        X_test = df.loc[test_mask, feature_cols]
        df_test = df.loc[test_mask]

        # Drop rows where label is NaN (end of series)
        valid = y_train.notna()
        X_train, y_train = X_train[valid], y_train[valid]

        if X_train.empty or X_test.empty:
            logger.warning("Fold %d: insufficient data, skipping.", i + 1)
            continue

        model = model_cls(**model_kwargs)
        model.fit(X_train, y_train)
        preds = pd.Series(model.predict(X_test), index=X_test.index)

        trades, equity = _simulate_trades(df_test, preds)
        metrics = compute_all(trades, equity)
        metrics["fold"] = i + 1
        metrics["test_start"] = str(ts.date())
        metrics["test_end"] = str(te.date())
        fold_results.append(metrics)
        all_trades.append(trades)
        all_equity.append(equity)
        logger.info("Fold %d | Sharpe %.2f | Win %.1f%% | DD %.1f%%",
                    i + 1, metrics["sharpe"], metrics["win_rate"] * 100, metrics["max_drawdown"] * 100)

    if not fold_results:
        raise RuntimeError("All folds were skipped.")

    # Chain equity curves continuously across folds
    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    chained, running = [], 1.0
    for eq in all_equity:
        scaled = eq * running
        chained.append(scaled)
        running = float(scaled.iloc[-1])
    combined_equity = pd.concat([e.reset_index(drop=True) for e in chained], ignore_index=True)
    summary = compute_all(combined_trades, combined_equity)
    summary["n_folds"] = len(fold_results)

    return {"folds": fold_results, "summary": summary}


def run_walk_forward_pretrained(df: pd.DataFrame, ensemble) -> dict:
    """Walk-forward backtest using a pre-trained ensemble — fast, no per-fold retraining.

    The ensemble was trained on 65K bars across 25 symbols. Here we split the test
    symbol's data into temporal folds and run inference only. The regime gate still
    applies — HIGH_VOL / UNKNOWN folds suppress signals.

    This is the correct approach when the model is trained on different stocks than
    the test symbol (no target leakage), and it completes in seconds not minutes.
    """
    dates = df.index
    fold_results = []
    all_trades, all_equity = [], []

    folds = []
    test_start = dates[int(len(dates) * 0.75)]
    for _ in range(WF_FOLDS):
        test_end = test_start + relativedelta(months=WF_FOLD_MONTHS)
        if test_end > dates[-1]:
            break
        folds.append((test_start, test_end))
        test_start = test_start + relativedelta(months=WF_STEP_MONTHS)

    if not folds:
        raise ValueError("Not enough data for walk-forward folds.")

    for i, (ts, te) in enumerate(folds):
        test_mask = (dates >= ts) & (dates < te)
        df_test = df.loc[test_mask]

        if df_test.empty:
            logger.warning("Fold %d: no test data, skipping.", i + 1)
            continue

        result = ensemble.predict_with_confidence(df_test)
        blocked = result["regime"].isin(["HIGH_VOL", "UNKNOWN"])
        result.loc[blocked, "signal"] = "HOLD"

        trades, equity = _simulate_trades(df_test, result["signal"])
        metrics = compute_all(trades, equity)
        metrics["fold"] = i + 1
        metrics["test_start"] = str(ts.date())
        metrics["test_end"] = str(te.date())
        fold_results.append(metrics)
        all_trades.append(trades)
        all_equity.append(equity)
        logger.info("Fold %d | Sharpe %.2f | Win %.1f%% | DD %.1f%% | Trades %d",
                    i + 1, metrics["sharpe"], metrics["win_rate"] * 100,
                    metrics["max_drawdown"] * 100, metrics["n_trades"])

    if not fold_results:
        raise RuntimeError("All folds were skipped.")

    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    # Chain equity curves so each fold continues from the prior fold's ending value
    # (avoids artificial drawdowns from resetting to 1.0 at each fold boundary)
    chained = []
    running = 1.0
    for eq in all_equity:
        scaled = eq * running
        chained.append(scaled)
        running = float(scaled.iloc[-1])
    combined_equity = pd.concat([e.reset_index(drop=True) for e in chained], ignore_index=True)
    summary = compute_all(combined_trades, combined_equity)
    summary["n_folds"] = len(fold_results)
    return {"folds": fold_results, "summary": summary}


def run_walk_forward_ensemble(df: pd.DataFrame) -> dict:
    """Walk-forward backtest using the full Ensemble (XGBoost + LSTM + HMM + MetaModel).

    Trains a fresh Ensemble on preceding data for each fold so there is no data leakage.
    The regime gate (HIGH_VOL / UNKNOWN) blocks signals, improving Sharpe.
    """
    from models.ensemble import Ensemble

    labels = make_labels(df)
    dates = df.index
    fold_results = []
    all_trades, all_equity = [], []

    folds = []
    test_start = dates[int(len(dates) * 0.75)]  # first 75% always training — folds land in 2023-2025
    for _ in range(WF_FOLDS):
        test_end = test_start + relativedelta(months=WF_FOLD_MONTHS)
        if test_end > dates[-1]:
            break
        folds.append((test_start, test_end))
        test_start = test_start + relativedelta(months=WF_STEP_MONTHS)

    if not folds:
        raise ValueError("Not enough data for walk-forward folds.")

    for i, (ts, te) in enumerate(folds):
        train_mask = dates < ts
        test_mask = (dates >= ts) & (dates < te)
        df_test = df.loc[test_mask]

        if df_test.empty:
            logger.warning("Fold %d: no test data, skipping.", i + 1)
            continue

        # Build multi-stock training set: all stored symbols with data before test start
        # This is the correct leakage-free approach — the model never sees any data >= ts
        try:
            from data.database import list_symbols, load_ohlcv
            from features.engineer import engineer_features
            frames = []
            for sym in list_symbols():
                if sym in ("^NSEI", "^INDIAVIX"):
                    continue  # macro context only — not training targets
                sym_df = load_ohlcv(sym)
                sym_df = sym_df[sym_df.index < ts]
                if len(sym_df) < 100:
                    continue
                try:
                    sym_feat = engineer_features(sym_df)
                    frames.append(sym_feat)
                except Exception:
                    pass
            df_train_multi = pd.concat(frames, ignore_index=False) if frames else df.loc[train_mask]
        except Exception as e:
            logger.warning("Multi-stock train build failed (%s), falling back to single stock.", e)
            df_train_multi = df.loc[train_mask]

        if len(df_train_multi) < 100:
            logger.warning("Fold %d: insufficient training data, skipping.", i + 1)
            continue

        ens = Ensemble()
        ens.fit(df_train_multi)

        result = ens.predict_with_confidence(df_test)
        # Regime gate: suppress signals in HIGH_VOL / UNKNOWN regimes
        blocked = result["regime"].isin(["HIGH_VOL", "UNKNOWN"])
        result.loc[blocked, "signal"] = "HOLD"

        trades, equity = _simulate_trades(df_test, result["signal"])
        metrics = compute_all(trades, equity)
        metrics["fold"] = i + 1
        metrics["test_start"] = str(ts.date())
        metrics["test_end"] = str(te.date())
        fold_results.append(metrics)
        all_trades.append(trades)
        all_equity.append(equity)
        logger.info("Fold %d | Sharpe %.2f | Win %.1f%% | DD %.1f%% | Trades %d",
                    i + 1, metrics["sharpe"], metrics["win_rate"] * 100,
                    metrics["max_drawdown"] * 100, metrics["n_trades"])

    if not fold_results:
        raise RuntimeError("All folds were skipped.")

    combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined_equity = pd.concat([e.reset_index(drop=True) for e in all_equity], ignore_index=True)
    summary = compute_all(combined_trades, combined_equity)
    summary["n_folds"] = len(fold_results)

    return {"folds": fold_results, "summary": summary}
