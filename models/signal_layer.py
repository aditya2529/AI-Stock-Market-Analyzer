"""Signal Layer — XGBoost classifier (BUY / HOLD / SELL)."""
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from xgboost import XGBClassifier
from sklearn.preprocessing import LabelEncoder
from config import RANDOM_STATE, MODELS_DIR, FEATURE_COLUMNS, INTRADAY_FEATURE_COLUMNS


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

    def _pick_columns(self, X: pd.DataFrame) -> list[str]:
        """Q4 fix: pick intraday cols when the DataFrame carries
        vwap_intraday (engineer_features added it because bars-per-day > 10),
        otherwise daily cols. Keeps a single trained-feature list per model."""
        cols_pref = INTRADAY_FEATURE_COLUMNS if "vwap_intraday" in X.columns else FEATURE_COLUMNS
        return [c for c in cols_pref if c in X.columns]

    def fit(self, X: pd.DataFrame, y: pd.Series):
        self._feature_cols = self._pick_columns(X)
        y_enc = self.encoder.transform(y)
        self.model.fit(X[self._feature_cols], y_enc,
                       eval_set=[(X[self._feature_cols], y_enc)],
                       verbose=False)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        cols = self._feature_cols or self._pick_columns(X)
        y_enc = self.model.predict(X[cols])
        return self.encoder.inverse_transform(y_enc)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Returns array of shape (n, 3) — [P(BUY), P(HOLD), P(SELL)]."""
        cols = self._feature_cols or self._pick_columns(X)
        return self.model.predict_proba(X[cols])

    def save(self, name: str = "signal_layer.pkl"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        # Q7 fix: also export the booster in version-portable UBJ format.
        # Pickle is xgboost-major-version-fragile; UBJ is forward/back compatible.
        try:
            booster_path = path.with_suffix(".ubj")
            self.model.get_booster().save_model(str(booster_path))
        except Exception:
            pass  # never let UBJ export fail the primary pickle save
        return path

    @classmethod
    def load(cls, name: str = "signal_layer.pkl") -> "SignalLayer":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
