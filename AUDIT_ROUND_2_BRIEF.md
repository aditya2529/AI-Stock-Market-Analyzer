# Audit Round 2 — Brief & Sequencing Guide
**Drafted:** May 15, 2026 09:50 IST · **Repo HEAD:** `cc3c756`

This is a **guided** brief — sequencing matters, and Round 1 made a mistake we are not repeating. Read this file fully before touching code.

---

## What we learned from Round 1 (May 14 evening)

Round 1 fixed 8 items cleanly and verified them. **But P9/P11 ("engine crash on Windows") was misdiagnosed by us, and the audit team trusted the diagnosis without independent verification.** The fix (UTF-8 encoding) was sound hygiene but addressed the wrong cause. Result: engine crashed **4 more times on Friday morning** (May 15, 09:15 / 09:25 / 09:35 / 09:45 IST), all with the same trigger profile.

**Mistake to avoid this round:**
1. Do not trust hypothesised root causes in P-items at face value. Reproduce the failure first.
2. Do not ship a fix without a regression test that *fails before* and *passes after*.
3. Do not bundle multiple unrelated fixes into one commit — bisect-friendliness matters when the next round needs to roll one back.

---

## Tier 1 — engine is currently unusable without watchdog (CRITICAL)

### P24 — heap corruption / access violation on BUY (Windows)
- **Symptom:** Python exits with `0xc0000374` (heap corruption) or `0xc0000005` (access violation) within 4–6 seconds of every BUY trade. C-level memory corruption in `ntdll.dll`, NOT a Python exception. UTF-8 fix from Round 1 had zero effect on this.
- **Evidence:** 4 crashes May 15 morning, 2 crashes May 14 afternoon, 100% correlation with BUY events. Two distinct exit codes from the same trigger ⇒ general memory-safety bug in C code reachable from the BUY path.
- **Do NOT just guess the cause. Bisect it.**

#### Required diagnostic sequence (in this order)

**Step 1 — Add C-level fault handler (5 min, zero risk):**
```python
# top of main.py, immediately after `import sys`
import faulthandler
faulthandler.enable(file=open('logs/faulthandler.log', 'a'))
```
This dumps Python's C-stack on segfault. The next crash gives us a real stack trace pointing to the actual frame. **Do not proceed to Step 2 until at least one crash has been captured with faulthandler enabled.**

**Step 2 — Bisect the trigger (cheapest first):**

A. Drop `max_workers=8` → `max_workers=1` in `intraday/engine.py:344`. Let engine run a full session. If crashes stop → race condition in C extension (most likely SHAP + xgboost shared state). If crashes continue → go to B.

B. Comment out the `generate_signal(...)` call in `intraday/engine.py:240` (the BUY-opened branch), replace with a minimal payload built from `signal_row` (no SHAP). If crashes stop → SHAP TreeExplainer is the cause. If crashes continue → go to C.

C. Comment out the `on_signal(alert_payload)` Telegram dispatch in `intraday/engine.py:250`. If crashes stop → urllib SSL in worker thread is the cause. If crashes continue → go to D.

D. Strip the BUY path down to ONLY `try_open()` + DB write. No SHAP, no Telegram, no payload formatting. If still crashes → the bug is in xgboost's `predict()` itself being called concurrently. If stops → reintroduce one component at a time.

**Step 3 — Write a regression test BEFORE the fix:**
A standalone test (`tests/test_p24_buy_path.py`) that opens 5 BUYs back-to-back in a thread pool of size 8, against a fixture ensemble, and asserts the process survives. This test should FAIL on current `cc3c756` and PASS on the fix.

**Step 4 — Apply the fix.** Whatever Step 2 surfaced. Possible directions:
- If thread race: serialize SHAP+predict calls with a `threading.Lock` shared across workers
- If SHAP: run `generate_signal` in a `subprocess`, return JSON over stdout
- If xgboost: pin to xgboost 2.x or upgrade to a build with the patch
- If urllib: switch to `requests` library or serialize Telegram sends through a queue+worker

**Step 5 — Verify.** Run the regression test from Step 3. Run the engine for at least 30 min during market hours and confirm zero `0xc0000374` or `0xc0000005` events in `Get-EventLog -LogName Application -Source "Application Error"` filtered to `python.exe`.

**Severity / urgency:** CRITICAL. The engine is currently functional only because the watchdog auto-restarts every 5 min. This drops alerts during dead windows and risks force-close failure at 15:15 IST (P20 fix mitigates but doesn't eliminate). Land Tier 1 before any other work.

---

## Tier 2 — correctness gaps (HIGH but not blocking)

### P25 — slot-aware position sizing
- Audit team's *original* P1 proposal included `remaining_slots` accounting. The shipped P1 fix dropped this. Today's data shows the gap is latent — risk-based sizing usually shields it — but a 5-cap-hit day would fully deploy NSE cash.
- **Fix:** complete the original P1 proposal. Specifically the `remaining_slots = max(1, INTRADAY_MAX_POSITIONS - open_count)` + `max_capital_per_trade = (mkt_equity / remaining_slots) * 0.80` block.
- **Regression test:** simulate 5 BUYs at cap, assert remaining NSE cash ≥ 5% of `nse_initial_cash`.
- **Sequence:** can land in parallel with P24, no dependency.

### P23 — Telegram alert delivery latency
- 9-min lag observed today (engine fired 09:15:08, phone received 09:24). HTTP layer is instant — lag is phone/Telegram-client side.
- **Code-side mitigation:** add a secondary push channel (Pushover or ntfy.sh) for trade alerts only. Telegram remains primary for non-urgent.
- **User-side mitigation (already advised):** whitelist Telegram bot in phone notification + battery settings.
- **Sequence:** lower priority than P24/P25, defer if time short.

---

## Tier 3 — defer unless Tier 1+2 lands fast

### P22 — startup Telegram heartbeat (15 LOC)
### P16 — NYSE cash phantom credit (~₹159K leak; schema reconciliation)
### P13 — universe scanner ERROR-spam on delisted symbols
### P15 — dashboard total may show combined NSE+NYSE (needs verification first)
### P19 — signal latency >1s (re-evaluate AFTER P24 — most likely a P24 downstream artifact)

### Items resolved by today's data (mark FIXED, no code work):
- **P17** — Telegram delivery silent-True was actually phone-side; superseded by P23.
- **P18** — already retracted in P11 amendment.

---

## Out of scope this round (DO NOT TOUCH)

- Any code in `models/`, `features/`, `signals/generator.py` *except* the audit-driven SHAP changes if Step 2 of P24 implicates it.
- `config.py` constants (`MAX_RISK_PCT`, `INTRADAY_MAX_POSITIONS`, `BROKERAGE_PCT`, `SLIPPAGE_PCT`).
- The 4–6 open `paper_positions` (engine is mid-session; ops side will manage).
- `INTRADAY_AUDIT.md`, `CLAUDE.md` — only update if a Tier 1 fix changes a documented constraint.

---

## Verification gates — required before each push

For every Tier 1 or Tier 2 fix:

1. **A regression test exists** that fails on parent commit and passes on the fix commit. Commit the test first, then the fix, in that order. (Lets us bisect.)
2. **`pytest tests/` runs green** — all existing 23 tests still pass.
3. **`python main.py review` runs without smoke** — at least the static-analysis style smoke is clean.
4. **For P24 specifically:** at least one full 30-min market-hours run with no entries in `Get-EventLog -LogName Application -Source "Application Error"` filtered to `python.exe`.
5. **Update `PENDING_AUDIT_FIXES.md`** with `**Status:** FIXED in <sha>` line, including the commit SHA of the fix (not the test).

---

## Constraints (carried over from Round 1)

- Watchdog stays enabled — belt and suspenders.
- One commit per item, `audit-fix: P# — short reason` style.
- Push to master per standard workflow.
- **If P24 turns out to be unfixable in this round** (e.g. requires xgboost upstream patch): document the workaround you tried, file the upstream bug link, and leave the watchdog-restart pattern in place as the operational mitigation. The ops side accepts this.
- Rollback target for emergencies: `cc3c756`.

---

## Time budget

This is a Friday-evening / weekend window. ~48 hours wall-clock. Tier 1 should consume the bulk of it. Tier 2 only if Tier 1 lands with verification time to spare.

**P24 alone may take a full day** — diagnosis-first is the explicit ask. Do not skip Steps 1-3.

---

## What the ops side will do during your round

1. Watch the engine. If watchdog can't keep up, ops will disable the scheduled task and notify you via a new P-item before market open Monday.
2. Manually close any positions stuck overnight via the same path used May 14 (`exit_reason="manual_force_close_p20"`).
3. File new observations as P26, P27, … if anything new surfaces during the live session.
4. NOT touch any code in `models/`, `signals/`, `features/`, `intraday/`, `paper_trading/` — that lane is yours.

---

## Deliverables

- Commits on master, one per item, properly prefixed.
- Regression tests committed BEFORE their corresponding fixes.
- `**Status:** FIXED in <sha>` lines in `PENDING_AUDIT_FIXES.md` for each item closed.
- A final summary commit on a clean tree before Monday 09:00 IST.
- If Tier 1 cannot be safely landed by Monday 06:00 IST: edit this brief with a "MONDAY: DO NOT RUN" note at the top and ops will disable the scheduled task.
