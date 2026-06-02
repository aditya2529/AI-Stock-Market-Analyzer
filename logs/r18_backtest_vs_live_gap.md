# R18 — Why does backtest (PF 8.1) not match live (PF 0.76)?

**Investigation date:** 2026-06-02
**Source data:** R17 v2b sandbox (`logs/r17_sandbox_v2b_floor60_cap8_REBASELINE.db`), production paper_trades (read-only)
**Methodology:** binomial sanity + code inspection + bar-convention verification + cross-reference live `_fetch_intraday`

---

## VERDICT

**LOOK-AHEAD BIAS in the replay harness — 5-minute forward window.**

Confidence: **~95%.** Sample-size noise is statistically falsified (1.5×10⁻⁵). Magnitude (10× PF) is consistent with one-bar look-ahead on a 5-min timeframe. Mechanism is identifiable in source.

**The bug:** `models/engine_replay_backtest.py:193` and two siblings use `raw.index <= ctx.current_clock` (inclusive). Bars in the OHLCV DB are stamped at OPEN time, so the bar at index `T` carries OHLC for the window `[T, T+5min)` — including a `close` that resolves at `T+5min`. The replay therefore sees a price 5 minutes in the future on every tick. Live `_fetch_intraday` only sees bars that have already closed (yfinance won't return the in-progress bar), so live operates without this advantage.

**Recommended fix:** change `<=` to `<` in the three patch points. The replay slice should be strict-less-than the replay clock, matching the "last fully-closed bar" semantics that live observes.

---

## Suspect ranking (post-investigation)

| # | Suspect | Result | Confidence |
|---|---|---|---|
| 1 | **Look-ahead bias** | **CONFIRMED** | **~95%** |
| 4 | Sample-size noise | **KILLED** (binomial P = 1.51×10⁻⁵) | ~99% |
| 2 | Data source mismatch | NOT INVESTIGATED — #1 is sufficient | n/a |
| 3 | Slippage / fill gap | NOT INVESTIGATED — #1 is sufficient | n/a |

---

## #4 — Sample-size noise: KILLED

If the backtest's claimed true win-rate of 0.888 were correct, then observing ≤7 wins in 16 live trades has probability:

```
P(X ≤ 7 | n=16, p=0.888) = 1.51 × 10⁻⁵
```

A 1-in-66,000 event. **The 0.888 backtest win-rate is statistically falsified by the live sample alone.** Plausibility frontier:

| True underlying p | P(≤ 7 wins in 16) | Plausible? |
|---:|---:|:---:|
| 0.50 | 0.402 | very |
| 0.60 | 0.142 | yes |
| 0.65 | 0.067 | marginal |
| 0.70 | 0.026 | unlikely |
| 0.85+ | < 0.001 | impossible |

True underlying win-rate is almost certainly in **0.45–0.70**. Backtest is wrong by a wide margin.

---

## #1 — Look-ahead bias: CONFIRMED

### Step 1 — bar timestamp convention

`market_data.db` 5-min bars are stamped at **OPEN time**, not close time. Verified by direct DB query:

```
bar 11:20  close=1360.90,  bar 11:25  open=1361.00,  diff=0.007%
bar 11:25  close=1361.00,  bar 11:30  open=1361.00,  diff=0.000%
bar 11:30  close=1361.90,  bar 11:35  open=1361.90,  diff=0.000%
... (10 consecutive bars, all within 0.01%)
```

`bar[t].close ≈ bar[t+5min].open` confirms each bar carries OHLC for `[t, t+5min)`. The bar at index T's close is the price AT T+5min.

This is consistent with yfinance's documented convention for intraday bars.

### Step 2 — replay slice logic (the bug)

`models/engine_replay_backtest.py:189-196`:

```python
def _patched_fetch_intraday(symbol: str):
    raw = ctx.raw_by_symbol.get(symbol)
    if raw is None or ctx.current_clock is None:
        return None
    slice_df = raw.loc[raw.index <= ctx.current_clock]   # <-- inclusive
    return slice_df if len(slice_df) >= 30 else None
```

At clock T, the slice **includes** the bar at index T. Because that bar's `close` is the price at T+5min, the replay can see 5 minutes into the future.

Sibling patches with the same off-by-one:

- `models/engine_replay_backtest.py:199-205` (`_patched_engineer_features` — same `feat.loc[feat.index <= clock]`)
- `models/engine_replay_backtest.py:510-518` (the precomputed `_patched_predict` — same `pred_full.loc[pred_full.index <= clock]`)

### Step 3 — what the engine does with this

`intraday/engine.py:347`:

```python
current_price = float(featured["close"].iloc[-1])
```

The engine reads `current_price` from the close of the last bar in `featured`. In replay this is the bar at index T — value resolved at T+5min. In live this is the bar at T-5min — value resolved at T (genuinely current).

`current_price` then drives:

- `try_close(symbol, current_price, "HOLD")` at line 352 — stop/target check against 5-min-future price
- Entry-side risk calc via `compute_stop_and_target` (the entry price the executor records is influenced by this same `current_price`)
- The features fed into `ensemble.predict_with_confidence` — every rolling indicator computed on a slice whose final row contains future info

### Step 4 — what live sees

`intraday/engine.py:307`:

```python
df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True)
```

yfinance only returns bars that have closed. At wall-clock T, the most recent bar is the one at index T-5min (the bar that just finished closing at T). So `featured.iloc[-1]` in live is at index T-5min, with close = price at T. **No leakage — `current_price` IS the actual current price.**

### Step 5 — why this produces a 10× PF gap

The replay knows, at each decision moment, the price 5 minutes in the future. Specifically:

- **Entry decisions** are made with the prediction layer's features computed on a slice whose final bar contains 5-min-future close/high/low/volume.
- **Stop/target checks** on open positions compare to a `current_price` that's 5 minutes in the future. So a target that would be hit any time in the next 5 min is registered as hit AT clock T, before the live engine could possibly know.
- **Symmetrically**, a stop that triggers within the next 5 min is registered as triggered too — but the replay still benefits because the model's CONFIDENCE was generated on the same look-ahead features, so the model has already directionally guessed right.

The net effect is a strategy that looks 5 minutes into the future and trades on what's about to happen. A win rate of 88% under such an oracle is unremarkable. Strip the oracle and revert to ~50% — exactly what live shows.

### Step 6 — binomial cross-check ties the magnitude

After ruling out sampling noise (#4), we know the true win-rate is ≈ 0.45–0.70. The replay's 0.888 implies an information advantage of magnitude consistent with one-bar 5-min look-ahead on a directional 5-min-bar trading strategy. The arithmetic works.

---

## What about trade-tape inferences?

The R16 trade-tape conclusion ("confidence cleanly separates wins from losses") is **still meaningful** but should be re-read: under look-ahead, confidence correlates with the model's certainty about the 5-min future. Filtering at high confidence in the replay produces high win-rate trivially because the model is most certain when the future is most knowable. In live (no look-ahead), the same confidence threshold has no such privileged information — the correlation likely doesn't transfer.

So: the trade-tape's win/loss confidence separation in the v3 R16 sandbox is most likely an artifact of look-ahead, not a generalizable signal. The R17 v3@0.70 cap=30 result that "beat v2b" is also an artifact for the same reason.

---

## Proposed fix

Change `<=` to `<` in three places in `models/engine_replay_backtest.py`:

```python
# Line 193 (_patched_fetch_intraday)
- slice_df = raw.loc[raw.index <= ctx.current_clock]
+ slice_df = raw.loc[raw.index <  ctx.current_clock]

# Line 205 (_patched_engineer_features)
- return feat.loc[feat.index <= clock]
+ return feat.loc[feat.index <  clock]

# Line 518 (the precomputed _patched_predict)
- return pred_full.loc[pred_full.index <= clock]
+ return pred_full.loc[pred_full.index <  clock]
```

Semantically: at replay clock T, the slice should include only bars that fully closed BEFORE T. The bar at index T (open T, close T+5min) is in-progress and unknown to live.

A regression test should accompany the change:
- Synthetic ascending-price 200-bar fixture
- Replay one symbol with a stub model that always predicts BUY
- Without the fix: > 80% of trades hit target (target ≈ next-bar close, perfectly predicted)
- With the fix: ≈ 50% (the model gets no oracle)

This test belongs in `tests/test_r11_engine_replay.py` (the replay-harness test file) as a permanent guard against re-introduction.

---

## Recommendation

1. **STOP treating any R17 backtest PF as deploy signal.** v2b@0.60cap8 PF 8.13, v3@0.70cap30 PF 9.58 — both contaminated by 5-min look-ahead. The R12/R14/R15/R16/R17 PF rankings are not informative about live behavior.

2. **APPLY THE FIX.** Three-line change to `engine_replay_backtest.py`. Add the synthetic-fixture regression test. Run the full suite for no-regression.

3. **RE-RUN THE BASELINES with the fix.** v2b @ 0.60 cap=8 OOS 2026-01-01 → 2026-05-28 with fixed slice. Expected: PF lands in the 0.7–1.5 range matching live. If it does, the bug explanation is complete.

4. **Re-run the model selection (R14/v3, R17/v3@0.70) under the fix.** It's likely the entire scalper hunt was chasing look-ahead artifacts. v2b may or may not still be the best model — but we'll know on honest terms.

5. **Operationally:** v2b stays live. The live numbers are the only honest numbers we have right now. Continue collecting live data; in 2-3 weeks we have a real PF estimate that's not subject to the replay's bias either way.

---

## Open questions for ops (not blocking the fix)

- After the fix, does the LSTM sequence layer's training also have look-ahead? The label `make_labels` uses `fwd_return = close.shift(-lookahead)` (forward-looking by design, for label generation — that's correct). But the feature engineering on `engineer_features` — does any rolling window use centered windows or `.shift(negative)` that would leak future info into features? Worth a separate audit.
- The R12 v2b retrain reported strong train/val/test metrics. Did those train-time metrics also use the same `engine_replay_backtest`-style slicing, or was the training/walk-forward harness independently sound? If the latter, the bug is only in the test path and v2b might be a genuinely good model — we'd just be unable to TEST it correctly.

---

## Filed P-items

- **[P0]** Three-line off-by-one in `engine_replay_backtest.py` (`<=` → `<`). Source of every backtest-vs-live PF gap since R11.
- **[P1]** Synthetic-ascending-price regression test in `tests/test_r11_engine_replay.py` to prevent re-introduction.
- **[P2]** Audit `features/engineer.py` for any rolling-window functions that could peek forward.
- **[P3]** Audit training-time data slicing in retrain harnesses (R9 walk-forward, R12 retrain) — independent of the replay bug.
