"""Meta Model — Logistic Regression that calibrates ensemble outputs."""
from __future__ import annotations
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import LabelEncoder
from config import RANDOM_STATE, MODELS_DIR

_LABEL_ORDER = ["BUY", "HOLD", "SELL"]

# Older saved sklearn models may be missing this attribute when loaded under a
# newer sklearn runtime. Add a class-level fallback so predict_proba can still
# resolve it even before an instance-level compatibility patch runs.
if not hasattr(LogisticRegression, "multi_class"):
    LogisticRegression.multi_class = "auto"


class MetaModel:
    """Takes stacked probability outputs from all layers → final signal."""

    def __init__(self, C: float = 1.0, max_iter: int = 1000):
        self.model = LogisticRegression(C=C, max_iter=max_iter, random_state=RANDOM_STATE,
                                        solver="lbfgs")
        self.encoder = LabelEncoder()
        self.encoder.fit(_LABEL_ORDER)

    def fit(self, layer_probas: list[np.ndarray], y: pd.Series) -> "MetaModel":
        """
        Args:
            layer_probas: List of (n, 3) proba arrays from each sub-model.
            y: Ground-truth labels (BUY/HOLD/SELL).
        """
        X_meta = np.hstack(layer_probas)
        y_enc = self.encoder.transform(y)
        self.model.fit(X_meta, y_enc)
        return self

    def _fix_compat(self):
        """Patch sklearn version mismatch — add missing 'multi_class' attribute."""
        if not hasattr(self.model, "multi_class"):
            self.model.multi_class = "auto"

    def predict(self, layer_probas: list[np.ndarray]) -> np.ndarray:
        self._fix_compat()
        X_meta = np.hstack(layer_probas)
        y_enc = self.model.predict(X_meta)
        return self.encoder.inverse_transform(y_enc)

    def predict_proba(self, layer_probas: list[np.ndarray]) -> np.ndarray:
        self._fix_compat()
        X_meta = np.hstack(layer_probas)
        return self.model.predict_proba(X_meta)

    def save(self, name: str = "meta_model.pkl"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def load(cls, name: str = "meta_model.pkl") -> "MetaModel":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
