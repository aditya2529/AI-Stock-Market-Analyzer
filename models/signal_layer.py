"""Signal Layer — XGBoost classifier (BUY / HOLD / SELL)."""
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from xgboost import XGBClassifier
from sklearn.preprocessing import LabelEncoder
from config import RANDOM_STATE, MODELS_DIR, FEATURE_COLUMNS


_LABEL_ORDER = ["BUY", "HOLD", "SELL"]


class SignalLayer:
    """Wraps XGBClassifier with label encoding and save/load."""

    def __init__(self, n_estimators: int = 300, max_depth: int = 6,
                 learning_rate: float = 0.05, subsample: float = 0.8,
                 colsample_bytree: float = 0.8, reg_alpha: float = 0.1,
                 reg_lambda: float = 1.0):
        self.model = XGBClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=learning_rate,
            subsample=subsample,
            colsample_bytree=colsample_bytree,
            reg_alpha=reg_alpha,
            reg_lambda=reg_lambda,
            random_state=RANDOM_STATE,
            eval_metric="mlogloss",
        )
        self.encoder = LabelEncoder()
        self.encoder.fit(_LABEL_ORDER)
        self._feature_cols = None

    def fit(self, X: pd.DataFrame, y: pd.Series):
        self._feature_cols = [c for c in FEATURE_COLUMNS if c in X.columns]
        y_enc = self.encoder.transform(y)
        self.model.fit(X[self._feature_cols], y_enc,
                       eval_set=[(X[self._feature_cols], y_enc)],
                       verbose=False)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        cols = self._feature_cols or [c for c in FEATURE_COLUMNS if c in X.columns]
        y_enc = self.model.predict(X[cols])
        return self.encoder.inverse_transform(y_enc)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Returns array of shape (n, 3) — [P(BUY), P(HOLD), P(SELL)]."""
        cols = self._feature_cols or [c for c in FEATURE_COLUMNS if c in X.columns]
        return self.model.predict_proba(X[cols])

    def save(self, name: str = "signal_layer.pkl"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def load(cls, name: str = "signal_layer.pkl") -> "SignalLayer":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
