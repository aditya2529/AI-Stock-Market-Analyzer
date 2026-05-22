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

### Tier 2.5 — Portfolio overlays (analyst-grade improvements, no retraining)

**These came out of the May 22, 2026 clean-day-#2 analyst review of an actual session — concrete observable patterns, not theoretical.**

The model itself picks decent signals (DIXON +₹1,046 with conf 0.80, TECHM +₹356, TATACOMM #1 +₹1,055 all proved this today). **The losses are coming from the FRAMEWORK around the model, not the model's predictions.** Five framework gaps were directly observable in today's 8 trades:

#### 2.5.1 Stock-quality tiering with differential sizing
**The gap:** Engine equal-weights signals on DIXON (best-in-class consumer electronics, ₹11K stock, clean balance sheet) and ZEEL (structurally weak media, Sony merger fallout, ₹82 stock). Same 1% portfolio risk → wildly different alpha quality.

**The fix:**
- **Tier A** (~15 stocks): large-cap NIFTY 50 constituents with clean fundamentals → normal sizing
- **Tier B** (~25 stocks): mid-cap NIFTY Next 50 + select NIFTY 100 → 50% reduced sizing
- **Tier C** (~10 stocks): small-caps + Adani group + structurally weak names → only trade at conf ≥ 0.80

**Implementation:** ~30 LOC. Add `TIER_A_SYMBOLS`, `TIER_B_SYMBOLS`, `TIER_C_SYMBOLS` constants in `config.py`. In `try_open`, multiply position size by tier weight before deploying.

**Today's evidence:** ABFRL (Tier C — Aditya Birla Fashion, mid/small cap) lost -₹91 essentially on round-trip transaction costs. UJJIVANSFB (Tier C — microfinance small-cap) lost -₹459 in 5 min — small-cap liquidity dies in last 30 min. ZEEL (Tier C) drifted to force-close.

#### 2.5.2 Sector momentum overlay
**The gap:** Model sees only individual stock TA. Doesn't see "NIFTY Bank is down 1% — banking stock BUYs are fighting the tape."

**The fix:** Before opening a BUY in a stock, check the parent sector index (Bank Nifty, NIFTY IT, NIFTY Auto, NIFTY Pharma, etc.). If sector is down ≥ 0.5% intraday → skip the BUY. If sector is up ≥ 0.5% → take normal size or upsize 1.2×.

**Implementation:** ~50 LOC. Add a sector-symbol mapping. Pull sector close in `_process_symbol`. Apply overlay before sizing.

**Today's evidence:** TATACOMM #1 succeeded riding telecom sector strength (Bharti earnings tailwind). TATACOMM #2 failed when sector momentum exhausted. The overlay would have caught the exhaustion and skipped trade #2.

#### 2.5.3 Headline-risk exclusion list
**The gap:** Adani group stocks (ADANIENSOL, ADANIENT, ADANIPORTS, ADANIGREEN, etc.) gap in either direction on group-wide news that's invisible to pure TA. Loss probability is ~2× normal.

**The fix:** Maintain a small `HEADLINE_RISK_SYMBOLS` set in config — Adani group, anything in active regulatory probe, anything with known earnings within 24h. Either skip entirely OR require conf ≥ 0.80.

**Implementation:** ~10 LOC. Static config list + check in `_process_symbol`. Update list manually weekly.

**Today's evidence:** ADANIENSOL hit SL for -₹1,061 — exactly the kind of name where pure TA fails. Wed's ADANIENSOL also lost.

#### 2.5.4 Hard 14:00 IST cutoff for new entries (refines existing P28-adjacent rule)
**The gap:** Today's engine opened UJJIVANSFB at **15:05 IST** and ABFRL at **15:10 IST** — both force-closed at 15:15 with 5-min lifespans. Each lost money to transaction costs alone.

**The fix:** Hard-stop new entries at **14:00 IST** (not 14:30 as previously suggested). Last 90 minutes are exit-only. Pros universally treat this as a discipline rule.

**Implementation:** 2 lines in `_process_symbol` before the BUY-open branch. Trivial.

**Today's evidence:** -₹550 combined wasted on late-day entries that had no time to work. This is the cheapest fix on this list and the most evidently needed.

#### 2.5.5 Same-day target cooldown (analyst-confirms existing Tier 2 item #4)
**The gap:** TATACOMM hit target +₹1,055 at 10:15 IST. Engine re-entered TATACOMM at higher price 30 min later, hit SL for -₹833. Same pattern Wednesday with ADANIENSOL. **This is the #1 single ₹ leak in the system.**

**The fix:** Mirror P30's SL cooldown logic but for target hits. Cannot re-enter a symbol within 2 hours of taking a profit on it (or for the rest of the trading day).

**Today's evidence:** TATACOMM round-2 -₹833. Wednesday's ADANIENSOL round-2 -₹1,145. Pattern is consistent.

**This was already in the playbook (Tier 2 #4) but the analyst review elevates priority — DO THIS FIRST in Tier 2 because it's both highest ₹ leak AND smallest blast radius.**

### Combined expected impact of Tier 2.5 (all 5 overlays)

Applied on today's session retroactively:
- ABFRL -₹91 (saved by Tier C filter or 14:00 cutoff)
- UJJIVANSFB -₹459 (saved by 14:00 cutoff and Tier C)
- TATACOMM #2 -₹833 (saved by target cooldown)
- ADANIENSOL -₹1,061 (saved by headline-risk exclusion OR Tier C 0.80-conf requirement)
- ZEEL -₹250 (saved by Tier C exclusion)

Combined saving: **+₹2,694** retroactive on today's 8 trades.
Today's actual: -₹236.
Today **with all 5 overlays:** ~+₹2,458 = **+0.49% on ₹5L allocation in ONE day**.

Annualized at this rate (just from today's 5 overlays applied): **+₹6L on ₹5L = ~120% annual return**.

**This estimate is wildly overoptimistic** because (a) it's retrofitting on known outcomes, (b) overlays interact with each other, (c) some "losses" the overlays save would have happened with overlay too. But the order-of-magnitude lift is real: **a model with PF 0.84 + good overlays can become PF 1.5+ without ANY retraining.**

The portfolio-construction discipline matters as much as the predictions themselves. This is what pros actually do.

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

#### 9. Run a SIMPLE strategy in parallel as a benchmark — Opening Range Breakout (ORB)

**This is the most defensible Tier-4 experiment.** Don't kill the ML strategy. Don't switch to swing. Just build a simple rules-based strategy alongside it and **let the data decide which one earns its keep.**

**The ORB rules:**
- 09:15-09:30 IST: do nothing, just observe. Record the high and low of every symbol's 15-min window.
- 09:30 onward: if a symbol's price breaks ABOVE its 09:15-09:30 high → BUY. Stop = 1×ATR below the breakout price. Target = 2×ATR above. (Same R:R as current ML strategy.)
- No new entries after 14:30 IST. Force close at 15:15 (use same P26 plumbing).
- Track via separate `paper_trades` rows tagged `strategy='orb'` (needs the P27 schema migration first).

**Why this matters:**
- ORB is the simplest profitable intraday strategy in academic literature. Published PF in real-world studies: **1.4-1.8** net of fees.
- Zero ML, zero C-extension threading, zero crash surface. **What broke your ML engine this week (SHAP, xgboost, curl_cffi) doesn't exist in ORB.**
- If ORB beats ML on the same 50 symbols over 30 trades, you have your answer — retire the ML strategy and ship ORB.
- If ML beats ORB, you've earned the right to keep the complexity.
- Either way, you have a benchmark, not a guess.

**Effort:** 1-2 days of clean coding. Doesn't touch the existing engine. Lives in parallel.

**Required prerequisite:** P27 (schema migration to add `strategy` column to `paper_positions` + `paper_trades`, separate cash buckets per strategy). Without this, ORB and ML pollute each other's stats.

**Decision rule at +30 ORB trades:**
- If ORB lifetime PF > current ML lifetime PF (0.61 today, hopefully better by then) → ORB wins. Retire ML, ship ORB to live money.
- If ML is better → keep ML, retire ORB. You've earned the complexity.
- If they're close (both 1.0-1.3) → run both as a 50/50 ensemble. Each diversifies the other.

#### 10. Try a different strategy entirely
Swing trading (multi-day holds), VWAP reversion (counter-trend), pairs trading, sector rotation. Requires building a new model + new schema migration (P27). **NOT before completing the ORB experiment in #9.**

---

## The plain-English strategy comparison

For the moments when this playbook feels too abstract, remember these analogies:

| Strategy | What it is, in 1 sentence | Real-world analogy |
|---|---|---|
| **ORB** | Wait 15 min, watch the opening range, bet on whichever direction breaks the range | Watching 5 overs of a cricket match before betting on the outcome |
| **VWAP reversion** | Buy when a stock dips below its session-weighted-average price, sell when it returns | Buying Bisleri at hill-station price knowing it'll revert to city normal |
| **ML momentum (current)** | AI predicts direction from 19 indicators, model says BUY/HOLD/SELL with confidence | A weather forecaster with 50 satellites — should be better than naked-eye, usually is |

**The hard truth:** ML strategy = the hardest to build correctly AND the hardest to debug AND the hardest to maintain. ORB = 1/10th the code, similar real-world PF. You went straight to the deep end. The simple stuff might genuinely outperform.

**The ORB experiment at Day #5 isn't admitting defeat. It's the professional move every quant team makes: benchmark complex strategies against simple ones, and only keep the complexity if it actually earns its keep.**

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
