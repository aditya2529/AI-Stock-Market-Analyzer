"""Regime Layer — Hidden Markov Model for market condition detection."""
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from hmmlearn.hmm import GaussianHMM
from config import RANDOM_STATE, MODELS_DIR


# 4 hidden states → mapped to regime names by volatility + trend rank
REGIME_NAMES = ["TRENDING_UP", "SIDEWAYS", "TRENDING_DOWN", "HIGH_VOL"]
N_STATES = 4


class RegimeLayer:

    def __init__(self, n_states: int = N_STATES, n_iter: int = 200):
        self.model = GaussianHMM(
            n_components=n_states,
            covariance_type="diag",
            n_iter=n_iter,
            random_state=RANDOM_STATE,
        )
        self.n_states = n_states
        self._state_map: dict[int, str] = {}

    def _observation_matrix(self, df: pd.DataFrame) -> np.ndarray:
        """Build (n, 3) observation matrix: [log_return, log_volume, atr_norm]."""
        log_ret = np.log(df["close"] / df["close"].shift(1)).fillna(0).values
        log_vol = np.log(df["volume"].clip(lower=1)).values
        # Normalise log_vol to zero mean
        log_vol -= log_vol.mean()
        atr_col = df["atr"].fillna(df["close"] * 0.01).values
        atr_norm = atr_col / (df["close"].values + 1e-9)
        return np.column_stack([log_ret, log_vol, atr_norm])

    def fit(self, df: pd.DataFrame) -> "RegimeLayer":
        X = self._observation_matrix(df)
        self.model.fit(X)
        self._build_state_map(df, X)
        return self

    def _build_state_map(self, df: pd.DataFrame, X: np.ndarray):
        """Map HMM states to regime names based on mean return & volatility."""
        states = self.model.predict(X)
        means = []
        vols = []
        for s in range(self.n_states):
            mask = states == s
            ret = X[mask, 0]
            means.append(ret.mean() if mask.any() else 0.0)
            vols.append(ret.std() if mask.any() else 0.0)

        # Assign regimes:
        #   highest vol → HIGH_VOL
        #   highest mean → TRENDING_UP
        #   lowest mean → TRENDING_DOWN
        #   remainder → SIDEWAYS
        sorted_by_vol = sorted(range(self.n_states), key=lambda s: vols[s])
        high_vol_state = sorted_by_vol[-1]
        remaining = [s for s in sorted_by_vol[:-1]]
        sorted_by_mean = sorted(remaining, key=lambda s: means[s])
        down_state = sorted_by_mean[0]
        up_state = sorted_by_mean[-1]
        sideways = [s for s in remaining if s not in (down_state, up_state)]

        self._state_map = {high_vol_state: "HIGH_VOL", up_state: "TRENDING_UP", down_state: "TRENDING_DOWN"}
        for s in sideways:
            self._state_map[s] = "SIDEWAYS"
        # Fill any gaps
        for s in range(self.n_states):
            self._state_map.setdefault(s, "SIDEWAYS")

    def predict_regimes(self, df: pd.DataFrame) -> pd.Series:
        """Return a Series of regime strings aligned to df's index."""
        X = self._observation_matrix(df)
        states = self.model.predict(X)
        return pd.Series([self._state_map.get(s, "UNKNOWN") for s in states], index=df.index)

    def predict_latest_regime(self, df: pd.DataFrame) -> str:
        return self.predict_regimes(df).iloc[-1]

    def save(self, name: str = "regime_layer.pkl"):
        path = Path(MODELS_DIR) / name
        with open(path, "wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def load(cls, name: str = "regime_layer.pkl") -> "RegimeLayer":
        path = Path(MODELS_DIR) / name
        with open(path, "rb") as f:
            return pickle.load(f)
