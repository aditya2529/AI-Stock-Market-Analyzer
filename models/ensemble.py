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

    def fit(self, df: pd.DataFrame,
            lookahead: int = None,
            buy_threshold: float = None,
            sell_threshold: float = None,
            vol_scaled: bool = False) -> "Ensemble":
        """Train all layers on df (must have feature columns + OHLCV).

        Uses first 80% for training, last 20% for meta-model calibration.
        Lookahead/thresholds override config defaults (used for intraday mode).
        vol_scaled=True (Q1 audit fix) anchors labels at 0.5x rolling sigma
        instead of a fixed pct — required on 5-min bars where the fixed
        threshold sits at the noise floor.
        """
        labels = make_labels(df, lookahead=lookahead,
                             buy_threshold=buy_threshold,
                             sell_threshold=sell_threshold,
                             vol_scaled=vol_scaled)
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

    def _meta_predict(self, layer_probas):
        """Call meta_model with sklearn version compatibility fix."""
        mm = self.meta_model
        if not hasattr(mm.model, "multi_class"):
            mm.model.multi_class = "auto"
        return mm.predict(layer_probas)

    def _meta_predict_proba(self, layer_probas):
        """Call meta_model.predict_proba with sklearn version compatibility fix."""
        mm = self.meta_model
        if not hasattr(mm.model, "multi_class"):
            mm.model.multi_class = "auto"
        return mm.predict_proba(layer_probas)

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Return Series of BUY/HOLD/SELL signals aligned to df's index."""
        sig_proba = self.signal_layer.predict_proba(df)
        seq_proba = self.sequence_layer.predict_proba(df)
        reg_proba = self._regime_to_proba(self.regime_layer.predict_regimes(df))
        preds = self._meta_predict([sig_proba, seq_proba, reg_proba])
        return pd.Series(preds, index=df.index, name="signal")

    def predict_with_confidence(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with [signal, confidence, regime] columns."""
        sig_proba = self.signal_layer.predict_proba(df)
        seq_proba = self.sequence_layer.predict_proba(df)
        regimes = self.regime_layer.predict_regimes(df)
        reg_proba = self._regime_to_proba(regimes)
        meta_proba = self._meta_predict_proba([sig_proba, seq_proba, reg_proba])
        labels = self._meta_predict([sig_proba, seq_proba, reg_proba])

        confidence = meta_proba.max(axis=1)
        return pd.DataFrame({
            "signal": labels,
            "confidence": confidence,
            "regime": regimes.values,
        }, index=df.index)

    def validate(self, df: pd.DataFrame, symbol: str = "VALIDATION") -> dict:
        """Run a quick sanity check on the last bar of df.

        Returns a dict with 'ok' (bool) and 'errors' (list of str).
        Checks: signal ∈ {BUY, HOLD, SELL}, confidence ∈ [0,1], price > 0.
        """
        errors = []
        try:
            result = self.predict_with_confidence(df.iloc[[-1]])
            row = result.iloc[0]
            signal = row["signal"]
            confidence = float(row["confidence"])
            # Price check — last close from raw df
            price = float(df["close"].iloc[-1]) if "close" in df.columns else -1

            if signal not in {"BUY", "HOLD", "SELL"}:
                errors.append(f"invalid signal '{signal}'")
            if not (0.0 <= confidence <= 1.0):
                errors.append(f"confidence out of range: {confidence:.4f}")
            if price <= 0:
                errors.append(f"non-positive price: {price}")
            if not (0.0 < confidence < 1.0) and signal != "HOLD":
                # degenerate: confidence exactly 0 or 1 is suspicious for non-HOLD
                errors.append(f"degenerate confidence={confidence:.4f} for signal={signal}")
        except Exception as e:
            errors.append(f"predict failed: {e}")

        ok = len(errors) == 0
        logger.info("Validation %s for %s — %s",
                    "PASSED" if ok else "FAILED", symbol,
                    "all checks OK" if ok else "; ".join(errors))
        return {"ok": ok, "errors": errors}

    def save(self, name: str = "ensemble.pkl", suffix: str = "",
             validate_df: pd.DataFrame = None, validate_symbol: str = "VALIDATION") -> Path:
        """Save ensemble to disk.

        If validate_df is provided:
          1. Run validate() first.
          2. If validation fails → raise RuntimeError (old model NOT overwritten).
          3. If validation passes → backup the existing file, then save.
        """
        fname = name.replace(".pkl", f"{suffix}.pkl") if suffix else name
        path = Path(MODELS_DIR) / fname

        if validate_df is not None:
            result = self.validate(validate_df, symbol=validate_symbol)
            if not result["ok"]:
                raise RuntimeError(
                    f"Ensemble validation FAILED — NOT saving to {path}.\n"
                    f"Errors: {'; '.join(result['errors'])}\n"
                    f"Existing model is untouched."
                )

        # Auto-backup existing model before overwriting
        if path.exists():
            from datetime import date
            backup_name = fname.replace(".pkl", f"_backup_{date.today().strftime('%Y%m%d')}.pkl")
            backup_path = Path(MODELS_DIR) / backup_name
            import shutil
            shutil.copy2(path, backup_path)
            logger.info("Backed up existing model → %s", backup_path.name)

        with open(path, "wb") as f:
            pickle.dump(self, f)

        # Q7 fix: also export the XGBoost booster in UBJ format alongside the
        # pickle. UBJ is version-portable across xgboost majors; pickle is not.
        # If the VPS ever runs a different xgboost version, load_booster_ubj()
        # can rebuild the signal layer without depending on pickle compatibility.
        try:
            ubj_path = path.with_suffix(".ubj")
            self.signal_layer.model.get_booster().save_model(str(ubj_path))
        except Exception as e:
            logger.warning("UBJ export failed (pickle still saved): %s", e)
        return path

    @classmethod
    def load(cls, name: str = "ensemble.pkl", suffix: str = "") -> "Ensemble":
        fname = name.replace(".pkl", f"{suffix}.pkl") if suffix else name
        path = Path(MODELS_DIR) / fname
        with open(path, "rb") as f:
            return pickle.load(f)
