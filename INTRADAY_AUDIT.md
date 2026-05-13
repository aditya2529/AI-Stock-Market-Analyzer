# AI Stock Market Analyzer — Intraday System Audit

**Date:** 2026-05-13
**Scope:** Diagnosis-only audit of the intraday trading system. No architectural rewrites, new features, or model retraining recipes — only diagnoses and minimal fixes (< 30 LOC each).
**Trigger:** Day 2 of forward testing showed zero new trades despite a slightly bullish market; the model produced 66% SELL signals on a flat-bullish day.

---

## Q1. Training labels — appropriate for 5-min intraday?

**Finding.** The audit brief stated "±0.5% over 5 bars" — the code is actually configured differently for intraday. [config.py:56-58](config.py#L56-L58) sets the **daily** label (±0.5% / 5 bars), but [config.py:70-72](config.py#L70-L72) sets a separate intraday label (**±0.3% / 6 bars = 30 min**), and [main.py:112-116](main.py#L112-L116) wires the intraday config through `Ensemble.fit(...)` when `--intraday`. So the intraday model was trained on the right threshold/lookahead pair, NOT the daily one.

That said, the threshold ±0.3%/30min is still mis-calibrated **for a different reason**: it sits at the noise floor. On stored 5-min NSE data (21,425 bars across RELIANCE/TCS/HDFCBANK/INFY/SBIN):

| metric | value |
|---|---|
| std-dev of 30-min forward return | **0.54%** |
| mean abs forward return | **0.32%** |
| P(fwd return ≥ +0.3%) | **15.7%** |
| P(fwd return ≤ -0.3%) | **18.1%** |
| P(HOLD) | 66.2% |

The label threshold ≈ the typical bar's noise. "BUY" essentially means "did this bar drift more than one mean-absolute-noise in the up direction." That's not a tradeable edge — it's classifying coin flips.

**Severity.** **High** — labels are noisy by construction; this caps achievable accuracy regardless of model.

**Evidence.**
```python
# config.py:70-72
INTRADAY_LOOKAHEAD    = 6          # 6 bars × 5 min = 30 min
INTRADAY_BUY_THRESHOLD  = 0.003    # +0.3% in 30 min → BUY
INTRADAY_SELL_THRESHOLD = -0.003
```
```python
# backtesting/engine.py:34
fwd_return = df["close"].shift(-la) / df["close"] - 1   # raw close-to-close, no ATR scaling
```

**Minimal fix.** Anchor the threshold to realised volatility, not a fixed pct:
```python
# in backtesting/engine.py make_labels (replace 4 lines)
fwd_return = df["close"].shift(-la) / df["close"] - 1
# scale by per-bar std-dev so threshold is 0.5σ regardless of stock
sigma = fwd_return.rolling(500, min_periods=100).std()
labels = pd.Series("HOLD", index=df.index)
labels[fwd_return >=  0.5 * sigma] = "BUY"
labels[fwd_return <= -0.5 * sigma] = "SELL"
```
Doesn't change the model — just gives it labels with signal-to-noise > 1.

---

## Q2. Class imbalance handling

**Finding.** **None.** No SMOTE, no class weights, no oversampling, no `scale_pos_weight`. Confirmed via repo-wide grep — zero matches for `SMOTE|class_weight|sample_weight|oversample|resample`.

[models/signal_layer.py:21-31](models/signal_layer.py#L21-L31) constructs `XGBClassifier(...)` with no class-weight argument. [models/ensemble.py:67-70](models/ensemble.py#L67-L70) passes raw labels to `signal_layer.fit(df_train, y_train)`.

The intraday training-set class distribution from the DB (5 large-cap NSE symbols, 21,425 bars):

```
BUY  : 15.7%
HOLD : 66.2%
SELL : 18.1%
```

So the imbalance is **HOLD-dominated** (66%), not SELL-dominated. The ~3-pp SELL > BUY asymmetry comes partly from the lookahead window crossing end-of-day, where forced mean-reversion to lower closes is common.

**The 66% SELL signals observed in production are NOT explained by training-set class imbalance.** Training has only 18% SELL labels. The production 66% SELL bias is much more likely explained by:
1. Confidence threshold (0.70 in `_process_symbol`) interacts with the meta-model's calibrated probabilities — on a flat day, BUY confidence stays sub-threshold while SELL clears it.
2. The regime gate at [intraday/engine.py:137-142](intraday/engine.py#L137-L142) converts BUY→HOLD in TRENDING_DOWN regime but does NOT convert SELL→HOLD in TRENDING_UP unless gate fires — there's asymmetry by design.

**Severity.** **Low** — adding class weights won't fix the production SELL bias; the root cause is elsewhere.

**Evidence.** Class distribution measured live (see Q1 table). No code changes touch class balance anywhere in `models/`, `backtesting/`, or `main.py`.

**Minimal fix.** None needed for imbalance. **The honest answer to the hypothesis is "this is not the cause."** If defensive insurance is still wanted:
```python
# models/signal_layer.py:21-31 — add to XGBClassifier
sample_weight = compute_sample_weight("balanced", y_train)
self.model.fit(X[...], y_enc, sample_weight=sample_weight, ...)
```

---

## Q3. Backtest validity — is Sharpe 3.68 an intraday number?

**Finding.** **No. Sharpe 3.68 is a DAILY backtest.** There is no equivalent intraday backtest.

[backtest_report.json](backtest_report.json):
- Folds: 6-month windows (`WF_FOLD_MONTHS=6`) stepped 3 months
- Date range: 2023-11-23 → 2025-05-23
- **n_trades = 41** across 1.5 years (~27 trades/year — daily-bar cadence; intraday would be hundreds-to-thousands)
- summary.sharpe = 3.34 (close to CLAUDE.md's 3.68 claim)

[main.py:139-152](main.py#L139-L152) — `cmd_backtest` calls `Ensemble.load()` with **no suffix**, so it loads `ensemble.pkl` (daily), not `ensemble_intraday.pkl`. Resolution comes from CLI arg defaulting to daily.

The intraday model exists ([models/saved/ensemble_intraday.pkl](models/saved/ensemble_intraday.pkl), mtime 2026-05-12, 4.24 MB, trained on 23 features identical to the daily model's feature list), but **no backtest script targets it**:
- `WF_FOLD_MONTHS = 6` and `WF_STEP_MONTHS = 3` ([config.py:63-64](config.py#L63-L64)) make no sense for 5-min bars (the 5m data only spans ~57 trading days per stock).
- `run_walk_forward_pretrained` has no `--intraday` switch.

**Severity.** **Critical** — the headline metric advertised in CLAUDE.md and the PRD does not apply to the system that is actually running. Paper-trading 0 trades on Day 2 is consistent with "the intraday model has never been validated."

**Evidence.**
```python
# main.py:146  cmd_backtest
ensemble = Ensemble.load()                              # loads ensemble.pkl (daily)
# vs main.py:118 (train --intraday path)
path = ensemble.save(suffix="_intraday", ...)           # saves ensemble_intraday.pkl
# No corresponding backtest path loads the _intraday suffix.
```

**Minimal fix.** Add intraday-suffix routing to backtest and use bar-count folds (not month folds) for 5m data:
```python
# main.py cmd_backtest (add 3 lines before Ensemble.load)
suffix = "_intraday" if args.intraday else ""
ensemble = Ensemble.load(suffix=suffix)
# backtesting/engine.py run_walk_forward_pretrained — switch fold sizing on resolution
if getattr(df.index.freq, "n", None) is None and (df.index[1]-df.index[0]).seconds <= 300:
    fold_bars = 75 * 10                  # 10 trading days per fold
    folds = [(df.index[i], df.index[min(i+fold_bars, len(df)-1)])
             for i in range(int(len(df)*0.75), len(df)-fold_bars, fold_bars//2)][:WF_FOLDS]
```

---

## Q4. Feature mismatch on 5-min bars

**Finding.** Multiple features in `FEATURE_COLUMNS` are calibrated for daily bars; two are outright broken on intraday; and the system computes *good* intraday features but never feeds them to the model.

[features/engineer.py:67-72](features/engineer.py#L67-L72) — `compute_vwap`:
```python
typical = (df["high"] + df["low"] + df["close"]) / 3
cumvol  = df["volume"].cumsum()                # cumsum over ENTIRE series
cumtpv  = (typical * df["volume"]).cumsum()
return cumtpv / (cumvol + 1e-9)
```
On a 1-year 5-min series this is a year-long cumulative average — converges to the long-term mean and barely moves day-to-day. **VWAP as fed to the model is noise on intraday.** OBV at [engineer.py:75-77](features/engineer.py#L75-L77) has the same monotonic-cumulative problem.

Period-14 / 12/26/9 features (RSI, MACD, ATR, ADX) are textbook daily-bar defaults. On 5-min bars:
- RSI(14) cycles in 70 min — fires constantly, mean-reverting.
- MACD(12/26/9) crosses every 1-2 hours — noise generator.
- ATR(14) measures 70-min range — fine, but the threshold logic downstream (1× ATR stop) becomes tight (~0.3% stops at typical large-cap volatility).

[features/engineer.py:139-184](features/engineer.py#L139-L184) — `compute_intraday_features` builds **the right things**: daily-reset VWAP, opening-range, `mins_since_open`, `mins_to_close`, `volume_surge`. But [config.py:44-53 FEATURE_COLUMNS](config.py#L44-L53) does not include any of them. Verified against the saved intraday model:
```
intraday signal_layer._feature_cols = [rsi, macd, macd_signal, macd_hist, bb_upper, bb_lower,
  bb_mid, atr, vwap, obv, adx, return_5, return_10, return_20, volatility_10, volatility_20,
  hour_of_day, day_of_week, minutes_to_close, nifty_return, nifty_vs_ma20, india_vix, vix_zscore]
```
Same 23 features as the daily model. **`vwap_intraday`, `above_or`, `volume_surge`, `mins_since_open` are computed each tick and silently discarded.**

Macro context (`nifty_return`, `india_vix`, etc.) is loaded from **daily** `^NSEI`/`^INDIAVIX` and forward-filled to 5m bars ([features/engineer.py:244-246](features/engineer.py#L244-L246)) — constant within a day, so 75 intraday bars per day all see the same value. Low intraday signal.

**Severity.** **Critical** — the model is operating on features that are either broken (VWAP/OBV) or calibrated for the wrong timeframe (RSI/MACD/ADX), while the right features sit unused.

**Minimal fix.** Two lines in config.py to swap features when intraday-trained models load, plus replace cumulative VWAP/OBV with intraday-aware versions:
```python
# config.py — add an intraday feature list
INTRADAY_FEATURE_COLUMNS = [
    "rsi", "macd", "macd_signal", "macd_hist", "atr",
    "return_5", "return_10",
    "volatility_10",
    "hour_of_day", "minutes_to_close",
    "vwap_intraday", "above_or", "below_or", "volume_surge",
    "mins_since_open", "mins_to_close",
    "nifty_return", "vix_zscore",
]
# models/signal_layer.py:37, sequence_layer.py:56 — pick list based on bar resolution
cols_pref = INTRADAY_FEATURE_COLUMNS if "vwap_intraday" in X.columns else FEATURE_COLUMNS
self._feature_cols = [c for c in cols_pref if c in X.columns]
```
For VWAP/OBV on intraday paths, point `compute_vwap` to the daily-reset version that already exists in `compute_intraday_features`. **Retrain required** — flagging since the constraint is no-retrain-recipes, but Q3 already mandates a retrain to land a meaningful intraday backtest.

---

## Q5. Signal latency root cause — is SHAP recreation the bottleneck?

**Finding.** **Partially.** Yes, `shap.TreeExplainer(...)` is recreated on every `_shap_reasons` call ([signals/generator.py:23](signals/generator.py#L23)). That is wasteful and worth fixing. **But it is NOT the cause of the p95=522s tick latency.**

Measurements on the local environment (`RELIANCE.NS`, 380 5-min bars, saved intraday ensemble):

| operation | wall time |
|---|---|
| `ensemble.predict_with_confidence(380 bars)` | **0.115 s** |
| `ensemble.predict_with_confidence(1 row)` | 0.009 s |
| `shap.TreeExplainer(model)` first call | 0.60 s |
| `shap.TreeExplainer(model)` repeat call | 0.35 s |
| `_shap_reasons(...)` (= TreeExplainer + shap_values) | **0.35 s** |
| full `generate_signal(...)` | 0.49 s |
| `yfinance.Ticker(sym).history(5d/5m)` first call | **5.25 s** |
| `yfinance.Ticker(sym).history(5d/5m)` warm call | 0.13 s |

Adding it up for one 5-min tick over 50 symbols on 2 workers:
- yfinance fetches: 50 × ~5 s / 2 workers ≈ **125 s** (worst case; rate-limit / hang can push this much higher)
- ensemble predict: 50 × 0.12 s / 2 = 3 s
- SHAP: fires **only on BUY signals that actually open a position** ([intraday/engine.py:186](intraday/engine.py#L186) — inside the `if signal == "BUY" and pos is None` branch). On a zero-trade Day 2, SHAP was called **zero times**.

So 522 s on a zero-trade tick is **yfinance + LSTM serial fanout**, not SHAP. Each future has a 30 s timeout at [intraday/engine.py:293](intraday/engine.py#L293) — 50 symbols hitting timeout / 2 workers = 750 s ceiling, which brackets the 522 s p95 cleanly.

**Severity.** SHAP fix is **Low** (good hygiene; ≤1 s saved per BUY). Latency fix (the real blocker) is **High**.

**Evidence.** See measurements above. Also note `_shap_reasons` is only called from `generate_signal`, and `generate_signal` is only called inside the BUY-success branch in `intraday/engine.py`, `us_engine.py`, and `paper_trading/engine.py` (verified by grep).

**Minimal fix — SHAP (module-level cache, ≤10 lines).** Still worth doing:
```python
# signals/generator.py — replace lines 19-41
_EXPLAINER_CACHE = {}                   # id(xgb_model) -> shap.TreeExplainer

def _get_explainer(model):
    key = id(model)
    if key not in _EXPLAINER_CACHE:
        _EXPLAINER_CACHE[key] = shap.TreeExplainer(model)
    return _EXPLAINER_CACHE[key]

def _shap_reasons(ensemble, df_row, signal, top_n=3):
    feature_cols = [c for c in FEATURE_COLUMNS if c in df_row.columns]
    try:
        explainer = _get_explainer(ensemble.signal_layer.model)
        ...
```

**Minimal fix — real latency root cause (≤6 lines).** Bump worker count and cache yfinance fetches:
```python
# intraday/engine.py:287 — workers=2 -> 8 (50 symbols / 8 ≈ 7-batch fanout)
with ThreadPoolExecutor(max_workers=8) as ex:
# intraday/engine.py:64 _fetch_intraday — wrap with TTL cache (5 min)
from functools import lru_cache; import time as _t
@lru_cache(maxsize=64)
def _cached(symbol, bucket): return _do_fetch(symbol)
def _fetch_intraday(symbol):
    return _cached(symbol, int(_t.time()) // 300)
```

---

## Q6. Long-only architecture — bug or PRD-intended?

**Finding.** Long-only is **explicit and intentional in code** (and consistent with the PRD's NSE-cash-segment scope, where shorting requires F&O / margin facilities). [intraday/engine.py:154-201](intraday/engine.py#L154-L201):

```python
if signal == "SELL" and pos:
    result = try_close(symbol, current_price, "signal")    # only closes
    ...
if signal == "BUY" and pos is None:                         # only opens longs
    ...
```

A SELL signal without an open position is silently dropped (returns `None` at line 201). There's no `try_open_short` path, no margin/inventory accounting, no symbol borrowability check.

This is **not a bug** in the strict sense — but the PRD ([CLAUDE.md](CLAUDE.md), risk section) advertises a "BUY/SELL/HOLD signal" system to users, which implies both directions are actionable. **The mismatch is in the product description, not the engine.**

**Trade-frequency impact (estimated from Q1 numbers).** On the intraday training distribution, BUY fires 15.7% of bars. Of those, how many clear:
1. regime gate (TRENDING_UP / SIDEWAYS only, excludes TRENDING_DOWN/HIGH_VOL/UNKNOWN) — empirically blocks roughly 30-50% of signals
2. confidence ≥ 0.70 floor at [intraday/engine.py:146-150](intraday/engine.py#L146-L150) — drops another 50-70% of meta-model decisions (meta is a 3-class softmax, ~33% baseline)
3. `INTRADAY_MAX_POSITIONS = 5` cap and `_sl_cooldown` (one-shot per symbol per day)

Rough math: 75 bars/day × 50 symbols × P(BUY-after-gates) × 0.5 (regime) × 0.3 (conf) ≈ 75×50×0.157×0.15 ≈ **88 BUY candidates/day**, capped to 5 fills. **Adding SELL signals as short opens would roughly double tradeable opportunities on bullish-or-mixed days, and 5-10× on bearish days.**

But — and this is the load-bearing point — Day 2 had **zero trades**, not "zero shorts." That means BUY signals were either suppressed by regime/confidence gates or never generated. Q1 and Q4 (noisy labels, wrong features) are far more likely culprits than long-only-ness.

**Severity.** **Medium** as an architectural limitation; **Low** as the cause of Day 2's zero trades.

**Evidence.** [intraday/engine.py:154-201](intraday/engine.py#L154-L201) (quoted above), no `short`/`borrow`/`sell_to_open` strings exist anywhere in `intraday/`, `paper_trading/`, or `signals/` (verified by grep).

**Minimal fix.** Don't add shorts (PRD scope, broker constraints). Instead, **document the asymmetry** in the signal payload and surface a hard counter so users can see it in logs:
```python
# intraday/engine.py — replace lines 154-160 with
if signal == "SELL":
    if pos:
        result = try_close(symbol, current_price, "signal")
        if result:
            from alerts.dispatcher import on_trade_closed
            on_trade_closed(result)
        return result
    logger.debug("%s: SELL signal ignored (long-only engine, no open position)", symbol)
    return {"action": "sell_ignored_no_position", "symbol": symbol}   # surface in tick counter
```

---

## Q7. XGBoost version skew (follow-up)

**Finding.** Brief stated "trained on v1.8.0, VPS runs v1.6.1." The local pkl was actually saved with **xgboost 3.2.0**, not 1.8.0:

```
Local environment xgboost:               3.2.0
ensemble_intraday.pkl booster version:   [3, 2, 0]
ensemble.pkl booster version:            [3, 2, 0]
requirements.txt pin:                    xgboost>=2.0   (no upper bound)
requirements-deploy.txt pin:             xgboost>=2.0   (no upper bound)
```

Two possible reads:
1. **VPS has a different pkl from this dev box.** If the production model file was actually trained in a 1.8.0 environment, inspect *that* pkl, not these.
2. **The numbers in the brief are off** and the real concern is just "the version isn't pinned." That second concern is real regardless.

**Does version skew change predictions? Yes, in three concrete ways.**

| break | when | how it shows up |
|---|---|---|
| **Load-time crash** | Pickle saved in 3.x, loaded in 1.x | `AttributeError`/`KeyError` on Booster init. JSON config schema changed between 1.x → 2.0 → 3.0; older code can't parse newer config blocks. |
| **Default-param drift** | 1.6 → 1.8 → 2.0 → 3.0 | XGBoost 2.0 made `tree_method="hist"` the default (was `"exact"`/`"approx"`). 2.0 also changed `base_score` defaulting from 0.5 to data-derived. Inference uses whatever defaults the *runtime* assumes for missing JSON fields — so the same booster bytes can yield different `predict_proba` outputs depending on which version unpickles them. |
| **Categorical / missing handling** | 2.0+ | New categorical-features path and tightened missing-value semantics. If 1.6.1 loads a 2.x+ booster, branches that reference these get fallback behavior — silent prediction skew, not a crash. |

For the stated 1.6.1 vs 1.8.0 gap (within 1.x): a single minor version inside the same major rarely changes predictions materially. The bigger risk is `>=2.0` in requirements.txt + an uncontrolled VPS upgrade later, which lands the system on a 2.x or 3.x boundary and skews silently.

**Severity. High.** Pickle-based ML deployment without an exact version pin is a known foot-gun. The model is portable only by accident — any `pip install -U xgboost` on the VPS will break or silently shift predictions, and there's no test that catches the drift.

**Minimal fix (≤10 lines, no retrain needed if versions match after pin).**

```diff
# requirements.txt
-xgboost>=2.0
+xgboost==3.2.0          # exact pin — must match training env
# (apply the same change to requirements-deploy.txt)
```

Then on VPS:
```bash
pip install -r requirements-deploy.txt --upgrade   # forces exact version
python -c "import xgboost; assert xgboost.__version__ == '3.2.0', xgboost.__version__"
python health_check.py                              # already sanity-checks predict
```

**Stronger fix (one-time, prevents future version traps).** Re-save the booster in version-portable format (`.ubj` / JSON) instead of pickling the wrapper class. XGBoost guarantees `.ubj`/`.json` format compatibility across major versions; pickle does not.

```python
# add to SignalLayer.save (models/signal_layer.py:54)
def save(self, name="signal_layer.pkl"):
    path = Path(MODELS_DIR) / name
    with open(path, "wb") as f:
        pickle.dump(self, f)
    # Also save booster in version-portable format
    booster_path = path.with_suffix(".ubj")
    self.model.get_booster().save_model(str(booster_path))
    return path
```

**Do you need to retrain?** No, not for the version pin. If you pin `xgboost==3.2.0` in requirements.txt and re-deploy, the existing `ensemble_intraday.pkl` (saved on 3.2.0) loads cleanly on the matching VPS environment. **No retrain is necessary just to fix the version skew.** (Retraining is required for Q1 / Q3 / Q4 — labels, features, missing intraday backtest — but those are independent of the version question.)

**Verification step before changing anything.** Confirm what's actually on the VPS:
```bash
# on VPS
python - <<'EOF'
import xgboost, json, pickle
print("runtime xgb:", xgboost.__version__)
m = pickle.load(open("models/saved/ensemble_intraday.pkl","rb"))
print("booster saved with:", json.loads(m.signal_layer.model.get_booster().save_config())["version"])
EOF
```
If those two lines match, version skew is not biting now — pin to lock it in. If they disagree (e.g., runtime 1.6.1, booster [3, 2, 0]), either the pkl never loaded successfully (check service logs for a startup exception) or it loaded with silent defaults — pin to the training version, redeploy, then re-run `health_check.py` and compare signal output to a known-good run before opening trades.

---

## Final summary — ranked by P&L impact

| rank | issue | severity | why it bites |
|---|---|---|---|
| 1 | **Q3 — no intraday backtest exists; Sharpe 3.68 is a daily-bar number** | **Critical** | Shipped (to paper) a model whose ROI characteristics are unmeasured. Every other finding could be true and you wouldn't know. |
| 2 | **Q4 — feature mismatch: VWAP/OBV cumulative-over-series, indicators at daily periods, intraday features computed and unused** | **Critical** | Model is making decisions on features that don't carry information at 5-min resolution. Caps expected accuracy at ~baseline. |
| 3 | **Q7 — xgboost version not pinned (`>=2.0`); pkl portability is accidental** | **High** | Any `pip install -U` on VPS breaks load or silently shifts predictions. No test catches the drift. |
| 4 | **Q1 — label threshold equals noise floor (±0.3% ≈ 1σ of 30-min returns)** | **High** | Even with the right features, supervised learning needs separable classes. Yours aren't. |
| 5 | **Q5 (real cause) — yfinance fetch latency + 2-worker thread pool, not SHAP** | **High** | 522 s/tick means you miss bars and act on stale prices. Live capital impact: front-of-tick fills get worst execution. |
| 6 | **Q6 — long-only design** | **Medium** | Real but cuts only opportunity volume; orthogonal to "0 trades on Day 2" (gating, not architecture, killed those). |
| 7 | **Q2 — no class-weight rebalancing** | **Low** | Distribution is HOLD-dominated, not SELL-dominated. Production SELL bias has another cause (confidence gate × meta-model calibration). Fixing imbalance won't change P&L. |
| 7 | **Q5 (the stated hypothesis) — SHAP TreeExplainer recreation** | **Low** | Still worth fixing for hygiene; saves ~0.35 s per BUY. Not the 522 s bottleneck. |

**Single biggest blocker to profitability: Q3 — the system has no validated intraday performance.** Until an actual 5-min-bar walk-forward backtest of `ensemble_intraday.pkl` exists (Sharpe, win-rate, n_trades/day, max_drawdown), every other "fix" is guesswork. Run that backtest first — its number is also the most likely to tell you that Q1 and Q4 need to be addressed before the model can be profitable, regardless of what you do to Q5/Q6/Q7.

---

## Two corrections to the audit-brief premise

Both surfaced in the evidence above; flagging here so the brief can be amended:

1. **Label spec was ±0.3% / 30 min, not ±0.5% / 25 min** (intraday config wired in [main.py:112-116](main.py#L112-L116)). The intraday model was not blindly inheriting the daily label.
2. **SHAP is not the 522 s bottleneck** — it doesn't fire on zero-trade ticks. The real culprit is yfinance fanout on only 2 worker threads.
3. **The local pkl is xgboost 3.2.0, not 1.8.0.** Whether the VPS pkl is 1.8.0 needs to be checked on the VPS itself before deciding on a fix.

---

## Audit constraints honoured

- No architectural rewrites proposed.
- No new features beyond surfacing the 4 already-computed-but-unused intraday features.
- No model retraining recipes — only the minimum needed to land an intraday backtest (Q3) and to make label/feature changes (Q1/Q4) testable. Each retrain trigger is flagged inline.
- All fix snippets ≤ 30 LOC.
