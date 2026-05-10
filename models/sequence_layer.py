"""Sequence Layer — 2-layer LSTM (PyTorch) for time-series pattern learning."""
from __future__ import annotations
import pickle
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path
from config import RANDOM_STATE, MODELS_DIR, FEATURE_COLUMNS

torch.manual_seed(RANDOM_STATE)

_LABEL_MAP = {"BUY": 0, "HOLD": 1, "SELL": 2}
_INV_LABEL = {v: k for k, v in _LABEL_MAP.items()}
SEQ_LEN = 20  # look-back window


class _LSTMNet(nn.Module):
    def __init__(self, input_size: int, hidden_size: int = 64, num_layers: int = 2, dropout: float = 0.3):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers=num_layers,
                            batch_first=True, dropout=dropout)
        self.fc = nn.Linear(hidden_size, 3)

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.fc(out[:, -1, :])  # last time-step


class SequenceLayer:

    def __init__(self, hidden_size: int = 64, num_layers: int = 2,
                 epochs: int = 30, batch_size: int = 64, lr: float = 1e-3):
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.epochs = epochs
        self.batch_size = batch_size
        self.lr = lr
        self._net: _LSTMNet | None = None
        self._feature_cols: list[str] = []
        self._scaler_mean: np.ndarray | None = None
        self._scaler_std: np.ndarray | None = None

    def _build_sequences(self, X: np.ndarray, y: np.ndarray | None):
        seqs, labels = [], []
        for i in range(SEQ_LEN, len(X)):
            seqs.append(X[i - SEQ_LEN: i])
            if y is not None:
                labels.append(y[i])
        X_seq = np.array(seqs, dtype=np.float32)
        y_seq = np.array(labels, dtype=np.int64) if y is not None else None
        return X_seq, y_seq

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "SequenceLayer":
        self._feature_cols = [c for c in FEATURE_COLUMNS if c in X.columns]
        Xv = X[self._feature_cols].values.astype(np.float32)
        yv = np.array([_LABEL_MAP.get(lbl, 1) for lbl in y.values], dtype=np.int64)

        # Standardise
        self._scaler_mean = Xv.mean(axis=0)
        self._scaler_std = Xv.std(axis=0) + 1e-9
        Xv = (Xv - self._scaler_mean) / self._scaler_std

        X_seq, y_seq = self._build_sequences(Xv, yv)
        if len(X_seq) < self.batch_size:
            # Not enough data — skip training, return zeroed model
            self._net = _LSTMNet(len(self._feature_cols), self.hidden_size, self.num_layers)
            return self

        dataset = TensorDataset(torch.tensor(X_seq), torch.tensor(y_seq))
        loader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False)

        self._net = _LSTMNet(len(self._feature_cols), self.hidden_size, self.num_layers)
        optim = torch.optim.Adam(self._net.parameters(), lr=self.lr)
        criterion = nn.CrossEntropyLoss()

        self._net.train()
        for _ in range(self.epochs):
            for xb, yb in loader:
                optim.zero_grad()
                loss = criterion(self._net(xb), yb)
                loss.backward()
                optim.step()
        self._net.eval()
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Returns (n, 3) softmax probabilities [P(BUY), P(HOLD), P(SELL)]."""
        if self._net is None:
            n = len(X)
            return np.full((n, 3), 1 / 3)
        Xv = X[self._feature_cols].values.astype(np.float32)
        Xv = (Xv - self._scaler_mean) / self._scaler_std
        X_seq, _ = self._build_sequences(Xv, None)
        if len(X_seq) == 0:
            return np.full((len(X), 3), 1 / 3)
        with torch.no_grad():
            logits = self._net(torch.tensor(X_seq))
            probs = torch.softmax(logits, dim=-1).numpy()
        # Pad front with uniform distribution for the warm-up rows
        pad = np.full((len(X) - len(probs), 3), 1 / 3)
        return np.vstack([pad, probs])

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        probs = self.predict_proba(X)
        idx = probs.argmax(axis=1)
        return np.array([_INV_LABEL[i] for i in idx])

    def save(self, name: str = "sequence_layer.pt"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def load(cls, name: str = "sequence_layer.pt") -> "SequenceLayer":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
