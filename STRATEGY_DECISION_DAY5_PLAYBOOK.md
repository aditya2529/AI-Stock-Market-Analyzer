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

**Committed path (chosen May 22, 2026): Option A — 4-week bundled rollout.**

User explicitly rejected the slower one-fix-at-a-time approach because of impatience. Accepted the trade-off: bundled fixes mean we can't isolate exactly WHICH overlay produced the PF lift, but we get the answer 6× faster.

### Pre-condition for kicking off

- 5 clean operational days accumulated (as of May 26 EOD: counter is at **4/5** —
  May 25 user-overridden as clean (3 restarts + DB reconcile, all planned + logged),
  May 26 was a TRUE clean day (no manual ops, no engine restart, watchdog quiet,
  force-close auto, 8 trades cycled cleanly, P44+P46 fixes held through full session).
  Need 1 more clean day before Day-5 verdict.)

**Important correction to original framing (May 26 evening):**
The brief assumed v1 was trained on multi-year 2014-2023 data and was therefore
"stale" with respect to 2024-2026 market regime. **That framing was wrong.**
Audit-team Round 7 investigation revealed:

- v1 actually trains on a rolling ~60-day yfinance window
- Refreshed monthly via `run_monthly_retrain.bat`
- So v1 is NOT calibrated on years-old data — it sees the most-recent ~2 months
- yfinance free tier caps 5m bars at 60 days, which is the structural reason
- DB currently holds only ~80 days of 5m bars total

**Consequence for the Day-5 framework:**
- "Retrain on fresh data" (Tier 2.5 step C) does NOT fix what we thought it fixed
  on yfinance data alone. v1 already sees fresh data.
- A retrain on more-recent slices would actually have LESS data than v1 → worse, not better
- The real fix is structural: procure 2+ years of 5m bars from Upstox v2
  historical-candle endpoint (logged as P48, Round 8 audit-team work)
- Once Upstox backfill lands (~mid-June), the retrain becomes meaningful as Round 9
- Tier 2.5 A + B (cooldown + 14:00 cutoff) ships Mon June 1 unchanged — they
  don't depend on the data-source fix and have empirical support (May 26 data
  showed B alone would have prevented 91% of today's loss)

This is a correction to the original strategy framework, not a new pivot. The
spirit of the Day-5 plan stands; only the C implementation path changes from
"retrain on stale-fixed data" to "retrain on Upstox-backfilled data".

---

**Timeline accelerated (May 26 night) — engine boots Wed May 27 with A+B early
AND fresh ledger from cutover-tonight:**

Audit team committed A+B changes directly to master locally before ops planned
cutover. Engine reads files from disk, so Wed auto-boot picks A+B regardless
of remote push state. User-accepted (Choice 2): let it run early instead of
rolling back. THEN user pushed back further (May 26 ~20:20 IST) — if A+B is
live tomorrow anyway, why not also reset the cash bucket tonight so Wed = Day 1
with a CLEAN ledger instead of preview-mode on an old one. Approved.

Cutover executed May 26 20:20 IST. Backup at
`market_data_pre_overlay_v1_20260526_202015.db` (183.3 MB). Archive tables:
`paper_trades_pre_overlay_v1` (66 rows), `paper_positions_pre_overlay_v1`
(0 rows), `paper_portfolio_log_pre_overlay_v1` (654 rows),
`paper_config_pre_overlay_v1`. Fresh state: nse_cash Rs 500,000, peak Rs 500,000,
all paper tables empty, 5 SL-cooldown keys wiped.

| Date | What |
|------|------|
| Wed May 27, 09:10 IST | **Strategy v2 Day 1 OFFICIAL** — engine boots with A+B + fresh Rs 500K ledger |
| Thu May 28 morning | Audit team cold-boot smoke test (sanity check, no state change) |
| ~Fri Jun 5 | After ~20 fresh-ledger trades, read v2 PF for the first time |
| ~Mid-Jun | If v2 PF >= 1.3 → strategy works, plan capital scale. If 1.0-1.3 → Week 2 overlays. If < 1.0 → diagnose. |

Clean Day #5 of the original framework is intentionally skipped — A+B is
empirically supported (May 26 data showed B alone would have prevented 91%
of that day's loss). Better to start fresh measurement with v2 now than
burn 2 more days running broken v1.

Rollback (if anything goes wrong tomorrow):
  1. Kill engine PID
  2. `copy market_data_pre_overlay_v1_20260526_202015.db market_data.db`
  3. `git revert 98ca061 8739c11` and push
  4. Re-launch engine via scheduled task
  Total downtime: ~5 min.
- Lifetime PF read at Day-5: if ≥ 1.5 → STOP, don't apply any overlays, just scale capital
- If lifetime PF < 1.5 → execute the 4-week sequence below

### The 4-week sequence

**Week 1 (~7-14 trading days):**
Apply Tier 2.5 overlays **#5 + #4 together** (the cheapest pair):
- **2.5.5 Same-day target cooldown** (~15 LOC, mirrors P30 pattern)
- **2.5.4 Hard 14:00 IST entry cutoff** (~2 LOC in `_process_symbol`)

Measure: 20 trades on the new code. Compute PF over those 20.

**Week 2:**
Apply Tier 2.5 overlays **#3 + #1 together** (the discipline pair):
- **2.5.3 Headline-risk exclusion list** (~10 LOC + config list)
- **2.5.1 Stock-quality tiering A/B/C** (~30 LOC + config lists)

Measure: 20 more trades. Re-compute PF.

**Week 3:**
Apply Tier 2.5 overlay **#2 (last one)**:
- **2.5.2 Sector momentum overlay** (~50 LOC, needs sector index data)

Measure: 20 more trades. Read final PF.

**Week 4 (decision week):**

| Lifetime PF after all overlays | Action |
|---|---|
| ≥ 1.5 sustained | Strategy works. Begin Phase 3 prep (broker, live small money). |
| 1.2-1.5 | Marginal. Continue with overlays as-is for 50 more trades. Don't add more. |
| < 1.2 | Model has structural issue. Build ORB in parallel (Tier 4). Compare 30 trades each. |
| < 1.0 | Strategy is genuinely losing money. Retire ML, ship ORB instead. |

### What user explicitly accepted by choosing Option A

- ❌ Lose the ability to attribute which specific overlay produced the lift
- ✓ Get the verdict 6× faster (4 weeks vs theoretical 24 weeks at one-fix-per-10-trades)
- ✓ Maintain measurement discipline across bundles (20 trades per bundle is statistically meaningful)
- ✓ Don't combine ALL overlays at once — still 3 measurement checkpoints

### Hard rules during the rollout

- **NO real money during these 4 weeks.** Paper only.
- **NO new strategy ideas** (swing, ORB) — finish this sequence first
- **NO config changes** outside the planned overlays (MAX_RISK_PCT, INTRADAY_MAX_POSITIONS, etc. stay)
- **NO emergency tweaks based on individual losing days** — only react to 20-trade aggregates
- **Daily losses up to -₹15K** (P28 circuit-breaker threshold) are NORMAL during testing. Don't panic.

### What ops Claude will do during these 4 weeks

- Watch engine health (zero crashes is the bar)
- Run weekly PF reports at end of each 20-trade bundle
- File any new operational bugs (new P-items)
- **NOT push strategy fixes** — those go through audit team
- **NOT relitigate** Option A (re-read this section if user wants to deviate)

### Why this is committed, not optional

The biggest risk to this plan isn't a fix that doesn't work — it's **abandoning the plan in week 2 because of one bad week of trades**. The playbook explicitly defends against this:

**If at any week the user wants to deviate (swing pivot, kill strategy, add real money, change config):**
1. Re-read the commitment at the top of this document
2. Re-read this Option A section
3. Wait 24 hours before acting
4. If still wanting to deviate after 24 hours, document the reason here as a new section before changing anything

That's the discipline contract. Three months ago you didn't have this discipline. Now you do. **Use it.**

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
