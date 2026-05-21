# Strategy Decision Playbook — Clean Day #5
**Drafted:** May 21, 2026 · **For:** Aditya, after 5 clean operational days have accumulated

---

## The commitment (written down so it's not relitigated emotionally)

> *"At clean day #5, if PF is still <1.2, I will improve the strategy. Until then, no changes."*

**Clean day** = single PID, 0 crashes, 0 faulthandler growth, 0 manual ops, force-close fires automatically, alerts arrive <2 min late.

**Counter as of May 21:** 0/5 (today is the first candidate)

---

## Decision tree at Clean Day #5

Read the lifetime PF after 5 clean days have accumulated. Don't peek before day 5.

| Lifetime PF at day 5 | What to do | Why |
|---|---|---|
| **≥ 1.5** | Keep strategy. Scale capital cautiously. | Real edge proven. |
| **1.2–1.5** | Apply ONE fix (trailing stop). Measure 10 more trades. | On the edge — small tweak may push over the line. |
| **< 1.2** | Apply structural changes (see below). | Strategy doesn't have edge as-is. |
| **< 1.0** | Consider retiring this model entirely. Move to a different architecture or strategy. | Losing money after fees. |

---

## Strategy improvement levers — ranked by impact × safety

Apply ONE AT A TIME. Measure 10 trades. Then decide on the next.

### Tier 1 — biggest leverage, lowest risk

#### 1. Trailing stop to breakeven
**What:** Once price moves +1×ATR in your favor, move SL up to entry price. Locks in no-loss.
**Expected PF lift:** +0.3 to +0.5
**Trade count effect:** 0% (doesn't filter any signals)
**Implementation:** ~30 LOC in `paper_trading/executor.py`. Add a `check_trailing_stop()` pass on every tick that updates `paper_positions.stop_loss` if price has moved favorably.
**Risk:** Tiny — additive only, doesn't change entry logic or risk budget.
**Why first:** Pure upside. Doesn't touch what the model was trained on.

**Today's evidence this would help:** TATACOMM made +₹842 (target hit). Without trailing stop, if TATACOMM had reversed at +0.8% before target, it would have given back to SL. Trailing stop captures the upward move regardless of reversal.

### Tier 2 — meaningful lift, small trade-count cost

#### 2. Confidence floor 0.60 → 0.70
**What:** Reject any signal with confidence below 0.70.
**Expected PF lift:** +0.2 to +0.4
**Trade count effect:** -50% (fewer but better trades)
**Implementation:** 1 line — change env var or default in `intraday/engine.py:173`.
**Risk:** Small — may starve the strategy of trades during quiet markets.

**Today's evidence:** MANKIND lost -₹1,890 at conf 0.63. Filter would have skipped it. But also would have skipped 2-3 winners that had conf 0.65-0.70.

#### 3. Regime filter — only trade TRENDING_UP
**What:** Reject BUYs when regime is SIDEWAYS or HIGH_VOL.
**Expected PF lift:** +0.2 to +0.4
**Trade count effect:** -30% (skips choppy regimes)
**Implementation:** ~10 LOC in `_process_symbol` — additional regime check.
**Risk:** Small — uses regime already in `signal_row`.

**Why valuable:** Chop hurts win rate AND PF (winners revert, losers fire fast). Skipping chop is pure upside if model regime detection is reliable.

#### 4. No re-entry same day after target hit
**What:** Add `target_cooldown_<date>` to `paper_config`, similar to `sl_cooldown_<date>` (P30). If a symbol hit target today, don't open again today.
**Expected PF lift:** +0.1 to +0.2
**Trade count effect:** -10%
**Implementation:** ~15 LOC, mirrors P30 pattern.

**Today's evidence:** ADANIENSOL hit target +₹867, then was re-entered after the first close and lost -₹1,145. Net ADANIENSOL today: -₹278. Cooldown would have kept the +₹867 winner clean.

### Tier 3 — structural changes (only if Tier 1+2 fail)

#### 5. Position size: 1% → 0.5% risk per trade
**What:** Halve the risk per trade. Halves both wins and losses.
**Expected PF lift:** 0 (PF is ratio, unchanged)
**Trade count effect:** 0%
**Why useful:** Smoother equity curve, smaller drawdowns. Doesn't fix PF but makes the system tradeable on smaller capital.

#### 6. Wider TPs — R:R 2:1 → 2.5:1 or 3:1
**What:** TP set further from entry. Bigger winners when they fire, but fewer winners (price has to move more to hit TP).
**Expected PF lift:** +0.1 to +0.3 (if winners are sticky)
**Trade count effect:** 0% on entries, lower WR (fewer targets hit, more time-out exits)
**Risk:** Medium — model was trained on 2:1 RR. Changing this is a deviation from backtest.

#### 7. Add cost-aware loss function in retraining
**What:** Retrain the meta-model with a custom loss that penalizes trades likely to lose money after fees.
**Expected PF lift:** +0.3 to +0.6
**Trade count effect:** -20% (model learns to skip marginal trades)
**Risk:** High — full retraining, takes 2-3 days, requires audit team.

### Tier 4 — nuclear options (only if everything above fails)

#### 8. Switch to a different model architecture
Drop LSTM, use only XGBoost + regime gate. Simpler, fewer moving parts.

#### 9. Try a different strategy entirely
Swing trading (multi-day holds) instead of intraday. Pairs trading. Sector rotation. Requires building a new model + new schema migration (P27).

---

## DO NOT do these (proven wrong)

- ❌ **First-30-min entry filter** — kills 90%+ of trades. The first 30 min IS the strategy. (Caught on May 21 — I had wrongly suggested this earlier.)
- ❌ **Change multiple things at once** — you lose the ability to measure which fix worked
- ❌ **Lower confidence floor below 0.60** — already too permissive, increases noise

---

## Order of operations at Day #5

1. Read lifetime PF, WR, max DD across all 5 clean days
2. If PF ≥ 1.5 → done, scale capital
3. If PF < 1.5 → apply **fix #1 (trailing stop)** only. Measure 10 more trades.
4. After 10 more trades: re-read PF. If improved → continue with fix #1, optionally add fix #3 (regime filter).
5. If still bad → fix #2 (confidence floor) instead, measure another 10.
6. Each fix gets its own 10-trade evaluation.
7. **Never combine fixes during the test phase.** Combine only AFTER each is independently proven.

---

## What this playbook is NOT

- ❌ Not a today-fix-it list
- ❌ Not a guarantee the strategy will reach PF 1.5
- ❌ Not a substitute for the discipline of waiting 5 clean days

It's a written commitment + a sequenced response so that when day #5 arrives, decisions are made from this playbook, **not from the emotion of whatever happened that morning.**

---

## Honest ceiling

Even if all Tier 1+2 fixes are applied successfully:
- Realistic best PF: **1.5–2.0** live
- Won't reach backtest's 3.73 (real execution slippage, real data delays, real fees)
- That's still genuinely profitable after fees if held for years

If after all of Tier 1+2 the lifetime PF is still <1.2 over 50+ trades:
- This specific strategy doesn't have edge
- Time to evolve the model (Tier 3+) or pivot to a different strategy (Tier 4)
- The PROJECT stays alive forever; only this specific STRATEGY retires

---

## Meta-rule

Every time you feel the urge to change strategy mid-week because of one bad trade, **re-read this file's commitment at the top**. The whole point of writing it down was to defend against that exact moment.
