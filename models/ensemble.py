"""Hybrid Ensemble Orchestrator.

Trains and coordinates all 4 layers:
    1. SignalLayer  (XGBoost)
    2. SequenceLayer (LSTM)
    3. RegimeLayer  (HMM)  — outputs regime string, not proba
    4. MetaModel    (Logistic Regression) — final calibration

Training sequence:
    fit_all(df_features, labels) →
        signal_layer.fit → sequence_layer.fit → regime_layer.fit → meta_model.fit
"""
import logging
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from config import MODELS_DIR, FEATURE_COLUMNS, LABEL_LOOKAHEAD, BUY_THRESHOLD, SELL_THRESHOLD
from models.signal_layer import SignalLayer
from models.sequence_layer import SequenceLayer
from models.regime_layer import RegimeLayer
from models.meta_model import MetaModel
from backtesting.engine import make_labels

logger = logging.getLogger(__name__)

_REGIME_PROBA = {
    "TRENDING_UP":   [0.60, 0.25, 0.15],
    "SIDEWAYS":      [0.30, 0.50, 0.20],
    "TRENDING_DOWN": [0.15, 0.25, 0.60],
    "HIGH_VOL":      [0.25, 0.50, 0.25],
    "UNKNOWN":       [0.33, 0.34, 0.33],
}


class Ensemble:

    def __init__(self):
        self.signal_layer = SignalLayer()
        self.sequence_layer = SequenceLayer()
        self.regime_layer = RegimeLayer()
        self.meta_model = MetaModel()
        self._fitted = False

    def _regime_to_proba(self, regimes: pd.Series) -> np.ndarray:
        """Convert regime strings to (n, 3) soft probability array."""
        return np.array([_REGIME_PROBA.get(r, _REGIME_PROBA["UNKNOWN"]) for r in regimes])

    def fit(self, df: pd.DataFrame) -> "Ensemble":
        """Train all layers on df (must have feature columns + OHLCV).

        Uses first 80% for training, last 20% for meta-model calibration.
        """
        labels = make_labels(df)
        valid = labels.notna()
        df_v = df[valid]
        labels_v = labels[valid]

        split = int(len(df_v) * 0.8)
        df_train, df_cal = df_v.iloc[:split], df_v.iloc[split:]
        y_train, y_cal = labels_v.iloc[:split], labels_v.iloc[split:]

        logger.info("Training SignalLayer (XGBoost) …")
        self.signal_layer.fit(df_train, y_train)

        logger.info("Training SequenceLayer (LSTM) …")
        self.sequence_layer.fit(df_train, y_train)

        logger.info("Training RegimeLayer (HMM) …")
        self.regime_layer.fit(df_train)

        # Build meta-model inputs from calibration set
        logger.info("Calibrating MetaModel …")
        sig_proba = self.signal_layer.predict_proba(df_cal)
        seq_proba = self.sequence_layer.predict_proba(df_cal)
        reg_proba = self._regime_to_proba(self.regime_layer.predict_regimes(df_cal))
        self.meta_model.fit([sig_proba, seq_proba, reg_proba], y_cal)

        self._fitted = True
        logger.info("Ensemble training complete.")
        return self

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Return Series of BUY/HOLD/SELL signals aligned to df's index."""
        sig_proba = self.signal_layer.predict_proba(df)
        seq_proba = self.sequence_layer.predict_proba(df)
        reg_proba = self._regime_to_proba(self.regime_layer.predict_regimes(df))
        preds = self.meta_model.predict([sig_proba, seq_proba, reg_proba])
        return pd.Series(preds, index=df.index, name="signal")

    def predict_with_confidence(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with [signal, confidence, regime] columns."""
        sig_proba = self.signal_layer.predict_proba(df)
        seq_proba = self.sequence_layer.predict_proba(df)
        regimes = self.regime_layer.predict_regimes(df)
        reg_proba = self._regime_to_proba(regimes)
        meta_proba = self.meta_model.predict_proba([sig_proba, seq_proba, reg_proba])
        labels = self.meta_model.predict([sig_proba, seq_proba, reg_proba])

        confidence = meta_proba.max(axis=1)
        return pd.DataFrame({
            "signal": labels,
            "confidence": confidence,
            "regime": regimes.values,
        }, index=df.index)

    def save(self, name: str = "ensemble.pkl"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def load(cls, name: str = "ensemble.pkl") -> "Ensemble":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
