# Pending Audit Items — For Next Audit Round

Living document of issues surfaced after the first audit (May 13, 2026)
that need the scrum team's attention in a follow-up audit. Each entry
includes evidence and a proposed minimal fix.

---

## P1. Position sizer is not cash-aware (single trade eats 70% of capital)

**Status:** FIXED in df135b2 — engine now passes `get_market_cash(_market_of(symbol))` to `try_open`, so the 20% cap in `executor._position_size` applies per-market. Verified: NSE ₹5L → 38 shares × ₹2625 = ₹99,750 (20.0%); NSE ₹1L → 7 shares × ₹2625 = ₹18,375 (18.4%, was 71% pre-fix).

**Symptom (May 14):** First trade of the day — ADANIENT.NS BUY — opened
27 shares @ ₹2625 = ~₹70,886. NSE allocation was ₹1,00,000. So one
position consumed **71% of NSE capital**, leaving only ₹29K for the
remaining 4 position slots (INTRADAY_MAX_POSITIONS = 5).

**Root cause** (`paper_trading/executor.py` → `_position_size()`):

```python
risk_amount = portfolio_value * MAX_RISK_PCT   # 1% of portfolio
shares = int(risk_amount / risk_per_share)
max_shares = int(portfolio_value * 0.20 / entry_price)
return min(shares, max_shares)
```

Issues:
1. `portfolio_value` passed in = `get_cash()` which returns **combined NSE
   + NYSE cash** (engine in `intraday/engine.py` calls
   `try_open(symbol, signal_row, current_price, get_cash())`). For an
   NSE-only engine this is wrong — it sees larger pool than it can use.
2. The 20%-per-position cap is computed against combined portfolio, so a
   single trade can exceed 20% of the NSE-only allocation.
3. No reservation for the other 4 position slots. With 5 max positions,
   a sensible per-trade cap is ~20% of NSE equity, not "first one takes
   all available cash up to 20% of combined."

**Proposed fix (minimal, ~15 lines):**

```python
# executor.py:_position_size — rewrite to use NSE-only equity + slot reservation
def _position_size(entry_price, stop_loss, symbol, max_positions=5):
    from paper_trading.portfolio import get_market_cash, get_open_positions
    is_nse = symbol.endswith(".NS")
    mkt = "nse" if is_nse else "nyse"
    mkt_cash = get_market_cash(mkt)
    mkt_open_eq = sum(
        float(r["entry_price"]) * int(r["shares"])
        for _, r in get_open_positions().iterrows()
        if r["symbol"].endswith(".NS") == is_nse
    )
    mkt_equity = mkt_cash + mkt_open_eq
    open_count = sum(1 for _, r in get_open_positions().iterrows()
                     if r["symbol"].endswith(".NS") == is_nse)
    remaining_slots = max(1, max_positions - open_count)
    # Per-trade cap = NSE equity / remaining slots, leave 20% safety
    max_capital_per_trade = (mkt_equity / remaining_slots) * 0.80
    max_shares_by_capital = int(max_capital_per_trade / entry_price)
    # Risk-based shares (existing logic, but on per-market equity)
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return 0
    risk_amount = mkt_equity * MAX_RISK_PCT
    shares_by_risk = int(risk_amount / risk_per_share)
    return max(0, min(shares_by_risk, max_shares_by_capital))
```

**Severity:** High — concentrates risk + starves remaining slots of capital.

**Acceptance test:** With NSE ₹5L, 0 open positions, INTRADAY_MAX_POSITIONS=5:
each trade should size to ≤₹80K (5L / 5 × 0.80). With 1 already open: each
remaining trade ≤₹107K (4L_remaining_equity / 4 × 0.80).

---

## P2. `max_pos` counter in tick summary can stay > 0 even when DB has 0 open positions

**Symptom (May 14):** After manually clearing all open positions in the
DB, the next two tick summaries still showed `max_pos=2`. DB queried in
parallel confirmed 0 open positions.

**Suspected cause:** Race condition in 8-worker `ThreadPoolExecutor`
fanout — multiple workers count `get_open_positions()` before any one
of them inserts, but the counter increment fires later when SQLite has
caught up.

**Proposed investigation:** Add a debug line printing `len(positions)`
right inside the `max_positions reached` branch in `intraday/engine.py`
to confirm the snapshot value at decision time vs. the actual count.

**Severity:** Low — counter cosmetic, doesn't block real trades.

---

## P3. SIGNAL_MIN_CONFIDENCE floor of 0.70 is empirically too tight for the new intraday model

**Status:** FIXED in 70ae9c5 — default lowered to 0.60 in `intraday/engine.py`. `run-intraday.bat` was already setting the env var to 0.60 explicitly; the code default now matches production.

**Symptom (May 14, first 3 ticks after market open):**
- Tick 1: 24/50 symbols `conf_blocked` (below 0.70 floor)
- Tick 2: 24/50
- Tick 3: 22/50
- Top confidences observed: 0.65, 0.67, 0.70 (just barely)
- 0 trades opened until floor was manually lowered to 0.65

**After lowering to 0.65:** conf_blocked dropped to 12/50, first BUY
opened immediately (ADANIENT @ conf=0.70).

**Suspected cause:** The retrained intraday model produces calibrated
probabilities in a narrower band (0.4–0.75) than the daily model
(0.5–0.9 range). The 0.70 floor was inherited from daily config.

**Proposed fix:** Either (a) lower floor permanently to 0.65 in
`intraday/engine.py` default + document, or (b) recalibrate the meta-
model's softmax output so 0.70 has the same semantic confidence as
the daily model.

**Severity:** Medium — currently runs OK at 0.65 but the daily-vs-
intraday calibration mismatch should be properly understood, not
papered over with a magic number.

---

## P4. Engine reads `SIGNAL_MIN_CONFIDENCE` env var via `os.getenv` on every tick — works, but is not documented

**Observation:** `os.getenv("SIGNAL_MIN_CONFIDENCE", "0.70")` runs in
`_process_symbol` per symbol per tick. So changing the env var in the
process environment hot-patches the threshold without restart. But this
only works for changes set BEFORE the process started — not while it's
running (env is snapshot at fork time).

**Proposed fix:** Document this clearly in `CLAUDE.md`, or move to a
config-file-watched threshold so live tuning is possible.

**Severity:** Low.

---

## P5. Position-size cap of 20% (in `_position_size`) is unaware of `INTRADAY_MAX_POSITIONS = 5`

**Status:** FIXED in df135b2 — implicit. With per-market cash flowing into `_position_size`, the 20% cap now applies to NSE-only or NYSE-only equity, so 5 × 20% = 100% of per-market allocation is the natural ceiling. No standalone change required.

Mathematically, if max 5 positions and each sized at 20% of portfolio,
you can fully deploy capital with 5 trades — perfect. But if any
single trade exceeds 20% (P1 above), the remaining slots starve. The
cap and the max-position count should be linked, not independent
constants. See P1's proposed fix — they should be addressed together.

---

## P6. Dashboard System Health tab uses Linux-only APIs (`/proc/meminfo`, `systemctl`)

**Symptom (May 14):** Running locally on Windows, RAM / systemd fields
show "undefined" or "unknown". Engine status, heartbeat, market state
work fine (those are cross-platform).

**Proposed fix:** Use `psutil` for cross-platform RAM/CPU/disk; gate
the systemd-specific calls behind `platform.system() == "Linux"` and
fall back to process-name lookup via psutil for Windows/Mac.

**Severity:** Low — cosmetic, only affects Health tab display on
non-Linux machines. Trading-relevant data (positions, P&L, equity) all
work cross-platform.

---

## P7. Model is empirically too conservative — most signals are HOLD

**Symptom (May 14, post-fix run on laptop, 0.65 threshold):**
- Out of 50 symbols per tick, ~38 pass all gates (regime + confidence)
  but the model still outputs HOLD for them.
- Only ~10 symbols per tick produce BUY/SELL signals strong enough to
  evaluate; of those, half clear 0.65 confidence.
- Net result: 1 trade in first 35 minutes of trading. After lowering
  threshold to 0.60, expected 5-10 trades over the rest of the day.
- Backtest confirms this: 49 trades over 4 folds × ~10 trading days
  = ~1.2 trades/day. The conservatism is by design (vol-scaled labels
  + meta-model calibration).

**Decision needed (NOT a code fix, a strategy choice):**

1. **Accept it.** Backtest shows Sharpe 1.84, PF 3.73 with this cadence.
   Few but clean trades. Don't change anything.
2. **Loosen the label threshold from 0.5σ → 0.3σ** during retrain.
   Model sees more bars as BUY/SELL → fires more signals in production.
   Risk: re-introduces noise that vol-scaling was meant to remove.
3. **Hybrid "soft BUY" tier.** Keep full-size trades at conf ≥ 0.70;
   add quarter-size trades for conf 0.55-0.70. ~30 lines in
   `intraday/engine.py` + `_position_size`. Spreads risk over more
   positions. No retraining needed.

**Recommended sequencing:**
- Days 1-10 of paper trading: **collect data, change nothing.**
- After 30+ live paper trades: compute realized win-rate and PF.
- If win-rate ≥ 45% AND PF ≥ 1.5 → option 1 (keep as-is).
- If win-rate < 40% but trade count adequate → option 2 (retrain looser).
- If trade count too low to evaluate (< 1 trade/day average) → option 3
  (add soft-BUY tier).

**Severity:** Medium (strategic, not technical). Engine is functioning
as designed; question is whether design matches user's intraday
trading-frequency expectations.

---

## P8. Silent try/except around alert dispatch hides failures

**Status:** FIXED in 55d6e69 — `except Exception: pass` at engine.py:239 replaced with `logger.warning("%s: alert dispatch failed — %s", symbol, e, exc_info=True)`. Bare-except no longer hides the encoding crash documented under P9/P11.

**Location:** `intraday/engine.py` — the BUY-opened branch:

```python
opened = try_open(symbol, signal_row, current_price, get_cash())
if opened:
    try:
        from signals.generator import generate_signal
        alert_payload = generate_signal(...)
        # override fields ...
        from alerts.dispatcher import on_signal
        on_signal(alert_payload)
    except Exception:
        pass        # ← swallows ALL errors silently
```

**Symptom (May 14):** CIPLA.NS BUY opened at 10:10 IST but no Telegram
alert was received. Logs showed "Dispatching BUY alert for CIPLA.NS" —
which means the engine's dispatcher was called. But user did not get
the message in real-time. Manual replay of the exact payload sent
successfully. Most likely cause was Telegram-client notification miss
on phone (not our bug), but the bare `except: pass` means we'd never
know if it WAS a real bug.

**Proposed fix (3-line change):**

```python
except Exception as e:
    logger.warning("Alert dispatch failed for %s: %s", symbol, e, exc_info=True)
```

Plus in `alerts/dispatcher.py:on_signal`, log the send result:

```python
tg_ok = telegram_bot.send_signal_alert(payload)
em_ok = email_alert.send_signal_alert(payload)
if not tg_ok:
    logger.warning("Telegram send for %s returned False", sym)
if not tg_ok and not em_ok:
    logger.warning("All alert channels failed for %s %s", signal, sym)
```

**Severity:** Medium — affects observability + trust in the alerting
pipeline. Without this fix, the user has to manually scroll Telegram
to confirm each trade fired an alert.

---

## P9. Engine died silently at 10:10 IST after dispatching a BUY alert (Windows)

**Status:** FIXED in 55d6e69 — `PYTHONIOENCODING=utf-8` in `run-intraday.bat`, `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` in `main.py`, fallback portfolio print wrapped in `try/except`, and the silent `except Exception: pass` at engine.py:239 replaced with `logger.warning(... exc_info=True)`. Manual reproduction: `python -c "import sys; print(sys.stdout.encoding)"` with the env var set now prints `utf-8`, and `₹ 1,00,000` round-trips through stdout.

**Symptom (May 14):** Engine (PID 7928) running fine, opened CIPLA.NS
BUY at 10:10:04, log shows "Dispatching BUY alert for CIPLA.NS" — then
log file freezes. No tick summaries for 25 minutes, until manual
restart. Process gone from Task Manager.

**Suspected root cause:** Unicode encoding error when writing emoji or
₹ symbol to Windows console / log file. The Telegram message format
includes `₹`, `🟢`, `🛡️`, `🎯` etc. Windows default code page is cp1252
which cannot encode these — would raise `UnicodeEncodeError` if any
code path tries to print/log the formatted message.

Existing NSE-only tick print already uses `₹` and works, so the basic
print() path is OK. The crash must be elsewhere — possibly inside a
worker thread where stderr handling differs, or in `format_signal_message`
when the engine internally logs the payload for any reason.

**Proposed investigation:**
1. Add `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` and
   same for stderr at the top of `main.py`. This force-encodes all
   output to UTF-8 regardless of OS default.
2. Set `PYTHONIOENCODING=utf-8` env var in `run-intraday.bat`.
3. Wrap the engine's worker function in a top-level try/except that
   logs `traceback.format_exc()` to a sidecar file — so even a fatal
   exception in a thread is captured before the engine dies.

**Severity:** Critical on Windows deployment. Engine ran 40 minutes
before dying. Without supervision/restart, missing 5 hours of trading
on a normal day. systemd-style auto-restart doesn't exist on Windows
Task Scheduler — would need a watchdog batch loop.

**Workaround in place:** None. User is manually monitoring engine
liveness via Task Manager + log file mtime.

**Update (May 14, post-watchdog):** `NSE_Engine_Watchdog` task now runs
every 5 min during market hours, PID-based liveness check, auto-restart
+ Telegram alert on death. P9 root cause still unfixed — watchdog
papers over it.

---

## P10. P1 confirmed live with exact numbers (CIPLA, May 14)

**Status:** FIXED in df135b2 — root cause (P1) addressed; this entry is observation-only.

**Symptom (May 14, 10:10 IST):** CIPLA.NS BUY opened at ₹1417.34 ×
97 shares = **₹1,37,482 = 22.1% of NSE cash (₹6,22,350)**. The 20%
cap fired against the **combined** NSE+NYSE portfolio (₹760K × 20%
≈ ₹152K), then was trimmed to ₹137K by the 95%-of-cash safety net.

**Confirms P1 root cause:** `intraday/engine.py:224` passes
`get_cash()` (combined) to `try_open(...)`. The 20%-of-portfolio cap
in `executor.py:_position_size()` therefore allows a single NSE
trade to exceed 1/INTRADAY_MAX_POSITIONS of NSE-only equity.

**Live evidence > backtest evidence:** ADANIENT @ ₹1L gave 71%; CIPLA
@ ₹5L gave 22%. The percentage scaled almost linearly with combined
allocation, which is exactly the symptom predicted by P1.

**Severity:** High (same as P1). This is observation, not a new fix
item — P1's proposed rewrite resolves both.

---

## P11. P9 FIRED TODAY, TWICE — confirmed by watchdog log (amended 15:30 IST)

**Status:** FIXED in 55d6e69 — same root cause as P9; see P9 status for the encoding-layer fixes.

**AMENDMENT:** Earlier wording of P11 was wrong. I diagnosed P9 as
"latent / survived today" based on the intraday log showing the
10:10 CIPLA dispatch followed by continued ticks. The watchdog log
proves otherwise — engine PID 3796 ran fine all morning, then **died
at 15:10 IST** (right after dispatching the RAIN/ADANIENT alerts in
the closing flurry). Watchdog restarted as PID 15448. **That PID
also died at 15:15 IST**, within 5 minutes, after attempting to
dispatch the LICHSGFIN alert. Watchdog restarted again as PID 16688.

**Watchdog log evidence (`logs/watchdog.log`):**

```
[2026-05-14 15:05:02] OK - engine alive (PID 3796), heartbeat n/a
[2026-05-14 15:10:02] PROBLEM: Engine process is NOT running
[2026-05-14 15:10:04] Telegram alert sent
[2026-05-14 15:10:12] Restart OK - new PID 15448
[2026-05-14 15:15:02] PROBLEM: Engine process is NOT running
[2026-05-14 15:15:10] Restart OK - new PID 16688
[2026-05-14 15:25:02] OK - engine alive (PID 16688)
```

Two crashes in 5 minutes, both correlated with BUY alert dispatch
events — exact same trigger profile as yesterday's 10:10 IST crash.
**P9 is the real, recurring, production-breaking bug.** Not latent.
Not papered over. The watchdog is hiding it from the user's
perception by auto-restarting fast enough that they only see the
"restarted successfully" Telegram messages, not the silent failure.

**Severity (revised): Critical — confirmed P9 mechanism, with both
the watchdog Telegram pings and the failed BUY alerts as evidence
in production.** This is the highest-priority fix in the file.

---

## P11-OLD (retained for history). P9 is latent — today's survival is not evidence

*Superseded by amended P11 above. Retained for audit-team timeline reference.*

**Symptom (May 14):** Engine dispatched the CIPLA BUY alert at
10:10 IST (the same trigger point that killed it yesterday) and
stayed alive through to 11:45+ IST. No crash.

**Why this does not close P9:**
- No code change since yesterday's crash (`git log` confirms).
- `PYTHONIOENCODING` is not set in `ops/windows/run-intraday.bat`.
- The fallback `print()` at `intraday/engine.py:402–407` still uses
  `₹` and has no `try/except` wrapper.
- The bare `except Exception: pass` at `intraday/engine.py:239`
  still hides any alert-path errors.

The defect is non-deterministic (depends on stdout buffer state +
which characters land in cp1252-encodable range at runtime). One
good day is not a regression test.

**Recommendation:** Audit team should not deprioritise P9 based on
the May 14 survival. Apply the proposed `PYTHONIOENCODING=utf-8`
fix + wrap the fallback print + replace the bare `pass`.

**Severity:** Critical on Windows (unchanged from P9).

---

## P12. Heartbeat file is dead code on Windows

**Location:** `intraday/engine.py:20`

```python
HEARTBEAT_FILE = Path("/home/opc/health/intraday.heartbeat")
```

On Windows the `mkdir(parents=True, exist_ok=True)` call fails
(no `/home/opc` root) and is silently caught by the surrounding
`try/except Exception: pass`. The file is never written.

**Current impact:** Zero. The Windows `NSE_Engine_Watchdog` is
PID-based and ignores this file.

**Future risk:** If anyone ports the file-mtime watchdog from the
Linux VPS to Windows (or just checks "is the heartbeat fresh?"
from the dashboard), they will see a permanently stale path and
trust silent dead code.

**Proposed fix:** Make the path env-driven with a Windows-safe
default:

```python
import os as _os
HEARTBEAT_FILE = Path(_os.environ.get(
    "HEARTBEAT_FILE",
    str(Path(__file__).parent.parent / "logs" / "intraday.heartbeat")
))
```

VPS continues to work by setting the env var; Windows writes to
`logs/intraday.heartbeat` by default.

**Severity:** Low. Cosmetic / latent. File under "tidy-up" not
"reliability fix".

---

## P13. Universe scanner emits 3 ERROR-level lines on every startup

**Symptom (May 14, every engine start):**

```
ERROR | HTTP Error 404: Quote not found for symbol: TATAMOTORS.NS
ERROR | HTTP Error 404: Quote not found for symbol: PEL.NS
ERROR | HTTP Error 404: Quote not found for symbol: LTIM.NS
ERROR | 3 Failed downloads: ['TATAMOTORS.NS', 'LTIM.NS', 'PEL.NS']
```

**Cause:** All three have re-tickered or restructured on NSE:
- `TATAMOTORS.NS` — demerged into commercial-vehicle / passenger-
  vehicle entities; original ticker may need re-mapping.
- `LTIM.NS` — Larsen & Toubro Infotech / LTIMindtree merger;
  current ticker may differ.
- `PEL.NS` — Piramal Enterprises restructuring; verify current
  symbol.

The scanner correctly drops them from the universe (selected
50 symbols from 200 candidates), but logs 4 ERROR-level lines
per startup which pollute the signal-to-noise on log scans.

**Proposed fix:** Either (a) curate the seed list to remove
delisted/re-tickered symbols, (b) downgrade scanner 404s from
ERROR to WARNING, or (c) maintain a `symbol_aliases.json` that
maps old→new tickers and rewrites on fetch.

**Severity:** Low. Pure observability noise.

---

## P14. First successful target hit — baseline metric (info, no action)

**Trade (May 14, 11:40 IST):**
- Symbol: ADANIENT.NS
- Entry: 10:10 IST (first trade of the session)
- Exit reason: `target` (hit TP, not SL, not signal)
- P&L: **+₹789.85 net (+1.11%)** on 97 shares
- R:R held near 2.0 as advertised (slippage + brokerage within design)

**Why this is filed:** First non-trivial target-hit closure of the
live deployment. Useful baseline for the audit team to compare
future trades against when evaluating the P7 30-trade-gate decision.

**Severity:** Info only — no fix needed.

---

## P15. Dashboard total may show combined NSE+NYSE, not NSE-only

**Observation:** `paper_portfolio_log.total_value` row at 11:45 IST
shows ₹7,59,832 (combined cash + open eq across both markets).
Engine's on-screen print uses an NSE-only snapshot (lines 379–398),
but the **DB log is the authoritative source for `/api/portfolio`**.

If the dashboard's Total card reads `paper_portfolio_log.total_value`,
the user sees a combined number that mixes their NSE trading
position with the static NYSE cash float. This makes drawdown,
return %, and the "₹X invested" framing all wrong for NSE-only
strategy evaluation.

**Proposed investigation:**
1. Confirm what `api/routes/portfolio.py:get_portfolio()` returns.
2. If it returns the combined row, add an NSE-only / NYSE-only
   split — either a new endpoint or a per-market view toggle on
   the dashboard.

**Severity:** Medium. Affects how the user reads their own
performance. Not a trading-correctness bug; engine sizes and
executes correctly per-market.

---

## P16. NYSE cash bucket has an unexplained ~₹159K leak

**Symptom (May 14, 15:10 IST):** `paper_config` table state:

```
nse_initial_cash = 500,000
nyse_cash        = 258,950    ← should be ~100,000
nse_cash         =  61,616    (consistent with 4 open NSE positions)
initial_cash     = 600,000
peak_value       = 759,832
cash             = 100,000    ← legacy/unused column
```

NYSE has had **zero trades, ever** (`paper_trades` lifetime = 1 row,
ADANIENT.NS today). NYSE cash should equal nyse_initial_cash (presumed
₹100K). Instead it sits at ₹258,950 — ~₹159K of phantom credit.

Combined snapshot: cash (₹320,566) + open_eq (₹439,266) = ₹759,832,
vs. expected ₹600,000 baseline + ₹789 realized = ~₹600,789. Variance
matches the NYSE-bucket inflation.

**Suspected cause:** The May 14 NSE allocation bump (from ₹1L → ₹5L)
likely went through a code path that credited the combined cash field
or wrote to the wrong market bucket. `set_cash` / `set_market_cash`
plumbing should be audited.

**Severity:** Medium. Trading is unaffected — engine sizes against
`nse_cash` correctly via `get_market_cash("nse")`. But `peak_value`,
drawdown %, dashboard total, and any reporting that reads the
combined or NYSE figures will be wrong until reconciled.

**Verification step for audit team:**
1. Check `git log -p paper_trading/portfolio.py` for any `set_cash` /
   `set_market_cash` changes since the ₹5L bump.
2. Trace whatever script bumped the NSE allocation — likely a
   one-shot DB write that didn't zero/reset the NYSE bucket.
3. Reconcile: nse_cash + nyse_cash + sum(open_position_costs)
   should equal nse_initial_cash + nyse_initial_cash + sum(realized_pnl).

---

## P17. Telegram "Dispatching" logged but message not received

**Symptom (May 14, 15:05–15:10 IST):** Engine opened 3 BUY positions
(RAIN.NS, ADANIENT.NS re-entry, LICHSGFIN.NS) within 5 minutes. Log
shows the dispatch step reached:

```
INFO | Dispatching BUY alert for RAIN.NS (conf=0.62)
INFO | Dispatching BUY alert for ADANIENT.NS (conf=0.78)
INFO | Dispatching BUY alert for LICHSGFIN.NS (conf=0.70)
```

User did **not** receive any of these three on Telegram. No
`"All alert channels failed"` warning followed any of them — which
means at least one channel returned `True`. Email is unconfigured
(`ALERT_EMAIL_FROM=""`), so Telegram itself returned `True` despite
the message never arriving.

**Earlier alerts today were received** (CIPLA BUY at 10:10, ADANIENT
target close at 11:40), so the bot token / chat_id are not broken
account-wide.

**Suspected causes (ranked):**
1. `telegram_bot.send_signal_alert()` swallows HTTP non-2xx responses
   and still returns `True` for some code paths (worth a code read).
2. Telegram API rate-limit: 3 messages in 5 minutes after a long
   quiet period can trigger silent throttling; the API may return
   200 OK but defer/drop.
3. Phone-side notification suppression (silent mode, focus filter)
   — outside our control, but should still be visible in chat history.

**Proposed fix (combines with P8):**

```python
# alerts/telegram_bot.py:send_message
import json
def send_message(text: str) -> bool:
    ...
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            ok = bool(data.get("ok"))
            if not ok:
                logger.warning("Telegram API non-ok: %s", body[:300])
            return ok
    except Exception as e:
        logger.warning("Telegram send raised: %s", e)
        return False
```

Currently the function likely doesn't parse the `"ok"` field of the
Telegram response — fixing that gives true delivery confirmation.

**Severity:** Medium-High. Alerts are a primary observability channel.
Silent loss == user has to manually reconcile DB vs phone every day.

**User verification step:** Check Telegram chat history (not just
notifications) for the 3 alerts at ~15:05–15:10 IST. If they ARE in
the chat history, it's phone-side suppression. If NOT in the chat,
it's a send failure that returned `True` incorrectly.

---

## P18. RETRACTED — universe re-scan is correct startup behavior of a restarted engine

**Original symptom misread:** I observed `"Scanning universe …"`
lines appearing mid-session after BUY alerts and filed it as a
re-scan bug. The watchdog log proves the engine crashed twice
between 15:10 and 15:15 IST (see amended P11). Each restart is a
fresh process and correctly re-runs the universe scan as part of
its startup sequence — that's by design, not a bug.

**What the duplicated `"Scanning universe …"` lines in the
intraday log actually mean:** evidence of a restart, not a code
defect in the selector. They are useful as a forensic marker for
P9 crash timing.

**Action for audit team:** No fix needed under P18. Treat
duplicated startup log markers as a crash signal during log
review.

---

## P18-OLD (retained for history). Universe scanner re-runs mid-session

**Symptom (May 14, 15:05–15:10 IST):** Each new BUY in the closing
flurry is followed by:

```
INFO | Dispatching BUY alert for ADANIENT.NS (conf=0.78)
INFO | Scanning universe — fetching previous day data for 200 symbols …
ERROR | HTTP Error 404: ... TATAMOTORS.NS ...
ERROR | $LTIM.NS: possibly delisted ...
ERROR | $PEL.NS: possibly delisted ...
```

The universe selector is supposed to run **once at engine startup**
(09:15 IST), pick the top-50 from 200 candidates, then operate on
those 50 for the rest of the day. Today's log shows it firing
**after every BUY in the last hour** — at least 3 times mid-session.

**Side effects:**
- 25–30 seconds of Yahoo Finance API calls per re-scan
- 3 dead-ticker ERROR lines repeated each time (P13 amplified)
- Almost certainly the cause of the new `Signal latency 1.53s exceeds
  gate 1.0s` warnings (see P19) — scanner-latency leaking into the
  tick budget
- Burns rate-limit headroom on yfinance (we already have to back off
  if Yahoo flags us)

**Suspected trigger:** A code path that calls `select_universe()` or
similar when a BUY opens — possibly tied to dashboard refresh,
position-replacement logic, or a debug hook left in. Need a grep of
all callers of the universe-scan entry point.

**Proposed fix direction:** Universe selection should be guarded by
a `_universe_selected_today` flag (set on first run, cleared at
midnight or engine restart). Any non-startup caller should be either
removed or made explicit via a config flag.

**Severity:** High. Drives latency-gate breaches, error-log flood,
and unnecessary API load. Not a correctness bug today, but the
latency cost will get worse as universe grows.

---

## P19. Signal latency gate (1.0s) breached on multiple late-day ticks

**Symptom (May 14, 15:05+ IST):**

```
WARNING | Signal latency 1.53s exceeds gate 1.0s
INFO | Dispatching BUY alert for RAIN.NS (conf=0.62)
WARNING | Signal latency 1.36s exceeds gate 1.0s
INFO | Dispatching BUY alert for ADANIENT.NS (conf=0.78)
```

The gate exists somewhere in the signal pipeline (per the WARNING
text — need to grep for "Signal latency"). Today is the first time
it's tripped per log.

**Likely root cause:** P18 (mid-session universe re-scan adds
~30s of latency that bleeds into the next tick's signal generation).

**Severity:** Medium, but expected to resolve when P18 is fixed.
Tracked separately because:
- If P18 fix doesn't drop latency below 1.0s, there's a second cause
  worth finding.
- The gate may currently be advisory-only (logs a warning but still
  fires the alert). Audit team should confirm whether the gate ever
  *blocks* a signal — if it does, breaches under P18 could be
  killing trades silently.

**Proposed investigation:**
1. `grep -rn "Signal latency"` to locate the gate.
2. Confirm gate behaviour: log-only vs. block.
3. After P18 fix, re-run a full day and check whether latency stays
   below 1.0s.

---

## P20. Force-close did not run at 15:15 IST — 4 positions stuck open overnight (CRITICAL)

**Status:** FIXED in 8a82b22 — three layers landed:
1. `forced_closed_{YYYY-MM-DD}` is now persisted in `paper_config` after a successful close, so a watchdog restart inside the same session does not re-trigger force-close.
2. Engine startup scans `paper_positions`: if any are open AND the most recent `entry_time` (UTC → IST) is older than today AND market is closed → calls `_force_close_all()` and exits, refusing to start a new session with stale positions.
3. `_force_close_all()` is now wrapped in a top-level `try/except` that writes the full traceback to `logs/force_close_failure_YYYYMMDD_HHMMSS.log` and returns success/failure. Callers only persist the flag on `True`, so a partial close retries on the next tick instead of locking in a half-flat state.

Verified: `set_config('forced_closed_2026-05-14','1') → get_config(...,'0') == '1'`. The 4 manual_force_close_p20 rows in `paper_trades` remain intact as the audit trail.

**Symptom (May 14, 15:30+ IST):** Market closed at 15:30. The
engine's `_force_close_all()` is supposed to fire at 15:15 IST
(`INTRADAY_FORCE_CLOSE_TIME = (15, 15)`) to flatten every open
position before the session ends. **It did not.**

**DB state after market close:**

```
paper_positions (still OPEN at 15:30+):
  CIPLA.NS      entry 10:10 IST  ₹137K
  RAIN.NS       entry 15:05 IST  ₹124K
  ADANIENT.NS   entry 15:05 IST   ₹98K
  LICHSGFIN.NS  entry 15:10 IST   ₹80K
  TOTAL OPEN EQUITY: ₹439K
```

**Why it failed:** Per amended P11 + watchdog log, the engine
crashed at 15:10 (PID 3796) and 15:15 (PID 15448). The 15:15 crash
happened **at the same minute the force-close branch is checked**.
PID 15448 likely died before reaching `_should_force_close()` →
`_force_close_all()`. PID 16688 took over at 15:15:10, watchdog
confirms alive at 15:25, but `paper_positions` table shows nothing
was closed.

**Possible causes inside PID 16688:**
1. Engine entered the main loop, found market still "open" briefly
   (within 15:15-15:30 window), then `_should_force_close()`
   returned True but `_force_close_all()` itself crashed silently.
2. The `forced_closed` flag persistence — the file-level boolean
   doesn't survive a process restart. Should have been True after
   PID 3796 attempted force-close. New process starts with
   `forced_closed = False` and re-runs force-close — but it didn't.
3. Engine reached force-close but every `try_close` call returned
   None (price fetch failed near market close, fallback to entry
   price ran but logged silently).
4. Engine sat in pre-market wait branch waiting for "tomorrow's
   9:15" because `now < open_today` evaluated True after midnight
   logic.

**Consequence:**
- 4 NSE positions carrying ~₹439K notional are open in the paper
  DB overnight (or longer if Monday's startup also doesn't clean
  them up).
- Tomorrow's engine startup will see them via `get_open_positions()`
  and will continue to evaluate stops/targets against them — but
  these positions were sized for INTRADAY exit, not overnight gap
  risk. Stops will trigger off the gap open, not the intra-day
  drift they were designed for.
- The `nse_cash = ₹61,616` figure is now stuck low until these
  positions close, blocking new trades tomorrow morning.

**Proposed fix (multi-layered):**

1. **Make `forced_closed` persistent** — write it to `paper_config`
   keyed by date, so a restarted engine knows force-close still
   needs to run. Pseudocode:

   ```python
   today_key = f"forced_closed_{date.today().isoformat()}"
   forced_closed = get_config(today_key, "0") == "1"
   ...
   if _should_force_close() and not forced_closed:
       _force_close_all()
       set_config(today_key, "1")
       forced_closed = True
   ```

2. **At engine startup, if market is closed AND `_should_force_close()`
   would have returned True AND any positions are open** → force-close
   immediately, then exit. Catches the "engine respawned after force-
   close window passed" edge case.

3. **Wrap `_force_close_all()` in a top-level try/except** that
   logs the full traceback to a sidecar file — currently it silently
   returns without confirming success.

4. **Add a sanity check at the start of the next session** (9:10
   pre-market): if `get_open_positions()` is non-empty AND the most
   recent entry_time is yesterday, log WARNING and refuse to start
   the new day until manually cleared.

**Severity:** CRITICAL. This is the worst class of paper-trading
bug — it silently changes the strategy from intraday to overnight
hold, invalidating all backtest assumptions and risk-sizing.

**Immediate user action (manual, post-market):** Option A — manually
close the 4 positions in the DB at today's close price to preserve
intraday accounting integrity. Option B — let them ride and treat
tomorrow's exits as data points for "what happens to forced-stale
positions" (worse for evaluation, but no data loss).

---

## P17 (amended). Telegram alerts not received — explained by P9 crash mid-send

**Original P17 hypothesised the bug was in `telegram_bot.send_message`
returning True on non-ok HTTP.** The watchdog log proves the actual
mechanism: the engine crashed BETWEEN `dispatcher.on_signal()` logging
its `"Dispatching BUY alert for X"` INFO line and the HTTP POST to
Telegram completing. Process death mid-syscall.

The proposed fix in P17 (parse `"ok"` field, log HTTP failures) is
still good hygiene and worth doing — but it would not have helped
in today's case, because the failure happened upstream of the HTTP
call entirely.

**Treat P17 as a hygiene improvement, P11 (P9 amended) as the
root cause for today's missing alerts.**

---

## P21. Entry-side brokerage/slippage debited in P&L but not in cash flow

**Status:** FIXED in 3320995 — `paper_trading/portfolio.py:open_position` now debits `entry_price * shares * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)` from the per-market cash bucket, mirroring `close_position`'s `entry_cost_with_fees` formula. Cash flow now matches reported net P&L; the ₹663.19 round-trip drift on May 14's 5 trades is structurally eliminated.

**Symptom (May 14, post-cleanup):** After closing today's 5 trades,
`nse_cash` settled at ₹502,489.70 vs. the expected ₹501,826.51
(₹500,000 initial + ₹1,826.51 reported net P&L). The cash bucket is
**₹663.19 higher than the trade ledger says it should be**.

**Root cause:** Asymmetric fee handling between open and close:

```python
# paper_trading/portfolio.py:open_position (line 139)
cost = entry_price * shares          # NO fees applied to cash debit

# paper_trading/portfolio.py:close_position (lines 164-168, 184)
entry_cost_with_fees = entry_cost * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)
exit_proceeds_net    = gross_proceeds * (1 - BROKERAGE_PCT - SLIPPAGE_PCT)
net_pnl              = exit_proceeds_net - entry_cost_with_fees
# cash credited at close:
set_market_cash(market, cash + exit_proceeds_net)
```

The reported `net_pnl` subtracts entry-side fees, but those fees
never actually came out of cash. The cash bucket therefore grows by
`exit_proceeds_net - entry_cost`, while the P&L line shows
`exit_proceeds_net - entry_cost × (1+fees)`. Difference per trade =
`entry_cost × (BROKERAGE_PCT + SLIPPAGE_PCT)`.

**Verification (today's 5 trades, fees rate = 0.0013):**

| Trade | Entry cost | Entry-side fees |
|---|---:|---:|
| ADANIENT (morning) | ₹70,886.07 | ₹92.15 |
| CIPLA | ₹137,482.00 | ₹178.73 |
| RAIN | ₹124,372.71 | ₹161.68 |
| ADANIENT (pm) | ₹97,795.04 | ₹127.13 |
| LICHSGFIN | ₹79,616.42 | ₹103.50 |
| **Sum** | | **₹663.19** ✓ |

Matches the unexplained excess to the rupee.

**Proposed fix (one-line, in `open_position()`):**

```python
# Before
cost = entry_price * shares

# After
cost = entry_price * shares * (1 + BROKERAGE_PCT + SLIPPAGE_PCT)
```

This makes the cash flow match the P&L formula — real brokers debit
fees at order placement, not at close. Alternatively, fix the P&L
formula to match the current cash flow by dropping the `× (1+fees)`
on entry, but that under-reports true round-trip cost.

**Severity:** Low. Cumulative drift is small (~₹130 per ₹100K
notional trade). Across 30 trades = ~₹4K of phantom credit. Not
trading-correctness, but affects the realism of paper P&L and any
return-percentage reported to the user.

**Acceptance test:** After fix, `nse_cash` after a complete trade
cycle should equal `nse_initial_cash + sum(net_pnl)` to the rupee.

---

## P22. No startup / pre-market Telegram heartbeat — user has no way to confirm engine booted

**Status:** FIXED in b0f8306 — `intraday/engine.py:run_intraday_session` sends a one-shot Telegram message just after `init_paper_tables()` + the P20 stale-position guard + the forced-closed check, before the main loop begins. Payload includes IST timestamp, NSE cash, and symbol count. Wrapped in `try/except` so a Telegram outage at boot can never prevent the engine from starting. Independent of the 30-min engine pulse — fires exactly once per session.

**Symptom (May 15, 09:00 IST pre-market):** User opened the project
expecting "at least a health message from Telegram" confirming the
engine had started for the day. None arrived because none is coded.

**Current Telegram trigger inventory:**
- Engine pulse — every 6 ticks (~30 min) during market hours only.
  First fires at ~09:45 IST, 30 min after market open.
- BUY/SELL alert — only when a signal clears confidence floor.
- Trade-closed alert — on SL/target/signal exit.
- EOD portfolio summary — after 15:15 force-close.
- Watchdog death alert — only on engine crash.
- Test ping — manual via `python main.py alerts test`.

**The gap:**
- No "engine started" message at 09:10 IST when the scheduled task fires.
- No "pre-market — waiting for 09:15 open" reassurance.
- A silent engine that successfully booted looks identical from
  Telegram's side to an engine that crashed before its first tick.
  Watchdog covers the latter, but only at 5-min granularity AFTER
  market opens.

**User impact:** Forces the user to dashboard or DB query to confirm
the system is alive each morning. Defeats the point of Telegram
being the primary observability channel.

**Proposed fix (~15 LOC):**

1. In `intraday/engine.py` at the top of `run_intraday_session()`,
   immediately after `init_paper_tables()`, send a startup ping:

   ```python
   from alerts.telegram_bot import send_engine_pulse
   send_engine_pulse({
       "now": _ist_now().strftime("%H:%M"),
       "symbols_processed": 0,
       "open_positions": len(get_open_positions()),
       "new_today": 0, "closed_today": 0,
       "cash": float(get_market_cash("nse")),
       "total": float(get_market_cash("nse")),
       "drawdown_pct": 0.0,
       "last_tick_actions": 0,
       "ram_mb": None,
       "_status_prefix": "🟢 Engine started",  # extend formatter
   })
   ```

2. Extend `format_engine_pulse()` in `alerts/telegram_bot.py` to
   honour an optional `_status_prefix` field — when present, swap
   the leading 🤖 emoji + "Engine pulse" line for the prefix.

3. Verify on next session boot that user receives a Telegram within
   60 seconds of 09:10:00 IST containing fresh NSE cash + open
   position count.

**Alternative (lighter touch):** Add a `send_test_ping`-style "Engine
booted" two-liner instead of a full pulse. Less informative but
zero risk of formatting issues.

**Severity:** Medium — observability gap, not correctness. User-
visible quality-of-life fix.

**Acceptance test:** On next Mon-Fri 09:10 IST auto-boot, user receives
a Telegram message before market open (09:15 IST) confirming engine
is alive and showing NSE cash + open-position count.

---

## P23. Telegram alert delivery latency makes intraday alerts non-actionable

**Symptom (May 15, 09:15 IST):** Engine opened TATACOMM.NS BUY at
09:15:08 IST. Telegram dispatch logged at the same moment. User
received the Telegram notification on phone at **09:24 IST — 9
minutes late**.

**Diagnostic evidence:** Manual replay of the same payload via direct
Telegram API call (`urllib.request.urlopen`) returned `HTTP 200 |
ok: true | message_id: <int>` instantly. Telegram's servers accepted
both the engine's original message AND the replay without latency.

**This rules out:**
- Engine bug (PYTHONIOENCODING + P9/P11 fix working as designed)
- HTTP send failure (P17 hypothesis a, b — both ruled out)
- Bot token / chat_id misconfiguration (test_ping arrives instantly)

**Root cause is phone/network/Telegram-client side:**
- Telegram app likely backgrounded → using poll-based sync rather
  than FCM push, with intervals of several minutes.
- Android Doze mode / iOS Background App Refresh throttling.
- Possibly mobile network congestion at market-open hour.

**Why this matters for intraday:**
- TATACOMM SL is ₹1,667.85 (just ~₹9 below ₹1,676.58 entry).
- At 5-min bar granularity, price can move ~₹5-15 in 9 minutes.
- A 9-min-stale alert means the user is reacting to a position that
  may already have hit SL or moved past optimal exit — alert is
  informational at best, useless for any intervention.

**This is not a code fix.** The audit team's P9/P11 work was correct
for what it scoped. But P17/P23 together prove: even with the crash
fixed, the user-facing alert pipeline has a hidden second-stage
failure mode that's invisible from the server log.

**Proposed mitigations (user-side, no code):**
1. Whitelist the Telegram bot for "high-priority notifications" in
   Android/iOS notification settings.
2. Disable battery optimization for the Telegram app.
3. Ensure Telegram has "background app refresh" / unrestricted
   background data on the mobile network.
4. If on Android: check `Settings → Apps → Telegram → Battery →
   Unrestricted`.

**Proposed mitigations (code-side, secondary):**
1. Add a secondary fast channel — e.g. a webhook to a simpler
   push service (Pushover, ntfy.sh) that has stricter SLAs than
   Telegram client-side delivery.
2. Send alerts via SMS for trades above a confidence threshold
   (e.g. via Twilio). Higher cost, but SMS push is OS-native.
3. Add a "first-tick-after-trade-open" log entry that fires on the
   NEXT tick if a position was opened in the previous tick — at
   least the trade is visible in the dashboard immediately even if
   Telegram is slow.

**Severity:** Medium-High. Alerts that lag 9 min behind reality break
the core observability promise of the project. User received the
TATACOMM alert at 09:24 IST — by then the position had already been
held for one full 5-min bar without their awareness.

**Acceptance test:** Next 5 trades, measure end-to-end latency
(engine OPEN log timestamp → phone notification timestamp). Target
< 60s for at least 4 of 5 trades.

---

## P24. ROOT CAUSE OF P9/P11 — Windows heap corruption (ntdll), NOT Unicode (CRITICAL)

**Status:** FIXED in 2643b42 (test in 6f98d68) — two surgical changes:
1. `alerts/telegram_bot.py` preloads `ssl.create_default_context()` at module import time and passes the shared context to every `urllib.request.urlopen` call, so worker threads never race on Windows `_load_windows_store_certs`.
2. `data/database.py:load_ohlcv` opens its own `sqlite3.connect(..., check_same_thread=False)` per call and skips the `PRAGMA journal_mode=WAL` side effect on `get_connection()`. Removes the pandas `_fetchall_as_list` thread race captured in `logs/faulthandler.log`.

Regression test `tests/test_p24_buy_path.py` (3 cases) FAILS on parent commit (915890d) — `_SSL_CONTEXT` import fails — and PASSES on 2643b42. Full suite 34/34 green.

**Symptom (May 15, 09:15 + 09:25 IST):** Engine died TWICE this morning,
each death within 6 seconds of a successful BUY trade opening, with
the audit team's P9/P11 fix in place (PYTHONIOENCODING=utf-8,
`sys.stdout.reconfigure`, wrapped fallback print, replaced bare
`except: pass`).

**Engine timeline:**
```
09:15:08  Engine opens TATACOMM.NS BUY (97 shares, conf 0.69)
09:15:14  Engine PID dies   ← 6 seconds after BUY
09:20:02  Watchdog detects dead, restarts → new PID 20048
09:25:11  Engine opens ADANIPORTS.NS BUY (45 shares, conf 0.65)
09:25:15  Engine PID 20048 dies  ← 4 seconds after BUY
09:30:02  Watchdog detects dead, restarts → new PID 5884
```

**Smoking gun — Windows Application Event Log:**

```
Faulting application name: python.exe, version: 3.11.8150.1013
Faulting module name:      ntdll.dll
Exception code:            0xc0000374        ← STATUS_HEAP_CORRUPTION
Fault offset:              0x0000000000117175
```

Both crashes show the identical signature: `0xc0000374` in `ntdll.dll`
at offset `0x117175`. **This is heap corruption from a C extension,
not a Python exception.** That is why:
- No Python traceback in the intraday log
- The P9/P11 PYTHONIOENCODING fix had zero effect (this is not an
  encoding bug)
- The crash is silent and instant (OS kills process; Python has no
  opportunity to write stderr)
- The intraday log just stops mid-tick

**Why the original P9 hypothesis (Unicode) was plausible but wrong:**
The original P9 entry hypothesised UnicodeEncodeError because the
crash followed a BUY alert that contained ₹/emoji characters and the
log went silent. Both observations are still true, but the *cause* is
that the BUY path triggers heap corruption — not that emoji output
crashed Python.

**Suspect components (in the BUY path that doesn't run on HOLD ticks):**
1. **SHAP TreeExplainer** — `signals/generator.py` cached the explainer
   at module level (Q5 fix from May 13 audit). It's invoked on BUY
   only. Threaded calls into shap+xgboost C code are a known
   heap-corruption surface.
2. **xgboost 3.2.0 + Python 3.13 + Windows** — xgboost's binary wheels
   for 3.13 are recent; bug reports of heap issues on Windows exist
   in the 3.x series.
3. **urllib SSL handshake** in a worker thread — Telegram POST runs
   on the same thread as the BUY logic; OpenSSL on Windows in a
   ThreadPoolExecutor worker has a history of heap issues.
4. **8-worker ThreadPoolExecutor** (Q5 fix bumped from 2 → 8). Higher
   thread count amplifies any race condition in shared C state.

**Reproducibility:** Crash fires on virtually every BUY trade. 100%
correlation across 2 May 14 deaths + **4 May 15 deaths** (09:15, 09:25,
09:35, 09:45). Watchdog hides the user impact but the data is
unambiguous.

**AMENDMENT (May 15, 09:48 IST) — second exit code observed:**

```
09:15:14  Exception 0xc0000374 (STATUS_HEAP_CORRUPTION)
09:25:15  Exception 0xc0000374 (STATUS_HEAP_CORRUPTION)
09:35:17  Exception 0xc0000005 (ACCESS_VIOLATION)   ← different!
09:45:15  Exception 0xc0000374 (STATUS_HEAP_CORRUPTION)
```

The appearance of BOTH `0xc0000374` and `0xc0000005` strengthens
the C-extension diagnosis: it's not one specific bug, it's general
memory mishandling somewhere in the BUY-path C code. Likely
candidates:
- SHAP TreeExplainer is reading/writing past an array boundary
- xgboost Booster object is being mutated concurrently from two
  threads
- urllib's OpenSSL backend is racing with the main thread's
  Booster.predict

A single specific bug would fire one signature consistently; two
signatures from the same trigger point at "memory safety in C code
that's being called from multiple threads."

**Proposed investigation (audit team — fresh round):**

1. **Cheapest first:** drop `max_workers=8` → `max_workers=1` in
   `intraday/engine.py:344`. If crashes stop, race condition in C code
   is the cause. ~30s code change, full day of evidence in one trading
   session.

2. **If single-thread still crashes:** comment out `generate_signal()`
   call in the BUY-opened branch (engine.py:240) — replace with a
   minimal payload built from `signal_row`. This bypasses SHAP entirely.
   If crashes stop, SHAP TreeExplainer is the cause.

3. **If SHAP is the culprit:** options are (a) move SHAP computation
   to a separate process via `subprocess.run`, (b) downgrade xgboost
   to a pre-3.x version, (c) skip SHAP reasons in alert payload.

4. **If urllib is the culprit:** wrap Telegram send in a subprocess
   call too; or switch to `requests` library which has different
   SSL handling.

**Severity:** CRITICAL. Engine is functionally unusable without the
watchdog restart loop. Each death loses any ticks during the dead
window (5-10 min) and any signals that should have fired in that
window are gone. Eventually one of these crashes will hit at 15:15
when the new restart can't complete force-close in time again (today's
P20 fix mitigates but doesn't eliminate the risk).

**Acceptance test:** Engine runs for an entire trading session
(09:15 → 15:30 IST) opening at least 3 BUY trades without a single
heap corruption death in the Windows Event Log.

**Operational impact today (May 15):** P11 amendment from yesterday
("CRITICAL — confirmed firing twice today") now confirmed *thrice
more* — once each on May 14 and twice already on May 15 morning.
This is a recurring production-breaking bug that the May 14 audit
round did not actually fix.

---

## P25. Position sizing is not slot-aware — 5 close-SL signals would fully deploy NSE cash

**Status:** FIXED in dbdd077 (test in bf7d333) — `paper_trading/executor.py:_position_size` now accepts `symbol=` and `max_positions=` kwargs and, when a symbol is provided, replaces the flat 20% concentration cap with `(portfolio_value / remaining_slots) * 0.80`, counting only positions in the same market (NSE vs NYSE). `try_open` passes `symbol` through.

Result: with 5 slots and no open positions, cap = 16% of per-market equity; the 5th cap-bound BUY leaves ≥5% of `nse_initial_cash` as buffer (encoded in `tests/test_p25_slot_aware_sizing.py::test_five_back_to_back_buys_leave_buffer`). Regression test (5 cases) FAILS on parent (TypeError — no `symbol` kwarg) and PASSES on dbdd077.

**Symptom (May 15, 09:30 IST):** With 4 of 5 INTRADAY_MAX_POSITIONS
slots filled, NSE cash deployed = ₹295,253 (58.8%). 1 slot remains.
A 5th close-SL trade would size at the 20% cap of *current* NSE
equity = ~₹100K but the `cash * 0.95` safety net would scale it
down. After 5 trades on a tight-SL day, deployment could approach
100% of NSE cash with zero reserve.

**Current sizing logic** (`paper_trading/executor.py:_position_size`
+ `try_open`):

```python
# Gate 1: risk-budget
shares_by_risk = int(portfolio_value * MAX_RISK_PCT / sl_distance)
# Gate 2: 20% concentration cap
shares_by_cap = int(portfolio_value * 0.20 / entry_price)
shares = min(shares_by_risk, shares_by_cap)
# Gate 3: 95% cash safety
if fill * shares > available_cash:
    shares = int(available_cash * 0.95 / fill)
```

**The missing logic:** the audit team's *originally proposed* P1 fix
included `remaining_slots` accounting. The shipped fix dropped that:

```python
# Proposed but not shipped in df135b2:
open_count = sum(... for nse positions ...)
remaining_slots = max(1, INTRADAY_MAX_POSITIONS - open_count)
max_capital_per_trade = (mkt_equity / remaining_slots) * 0.80
```

This would mean: first trade ≤20% of NSE, second ≤25% of *remaining*
(80% / 4 slots = 20% of total), etc. Each trade reserves headroom for
the slots still to come.

**Today's data shows the gap is latent, not active:**
Most of today's trades sized by risk-budget (wide SL → smaller
position) so cap rarely binds. But TATACOMM did hit the cap. A day
of 5 tight-SL signals (typical of low-volatility regime) would
fully deploy at the cap on every trade.

**Severity:** Medium. Doesn't break trading today, but the safety
margin against a "5 cap-hits" day is thin. Worth landing alongside
the audit team's existing P1 work — completes the original proposal.

**Acceptance test:** Force a scenario with 5 simulated BUYs all
sized at the cap. After the 5th `try_open()`, NSE cash should be
≥ 5% of `nse_initial_cash`, not ~0.

---

## P26. `try_close` silently rejects force-close signals — root cause of P20 partial failure

**Status:** FIXED by ops side in a34497b (one-line patch to `paper_trading/executor.py`). Regression test added in 9970277 — `tests/test_p26_force_close.py` covers (a) the original bug (force-close between SL/TP), (b) priority preservation (SL still wins at the SL price), and (c) no over-broadening (HOLD between stops still returns None). Runs against a temp SQLite DB via `tmp_path` + monkeypatch on `data.database.DB_PATH`.

**Symptom (May 15, 15:15+ IST):** 3 positions (TATACOMM, SAIL, SYNGENE) remained
open after the 15:15 force-close window, even though
`paper_config.forced_closed_2026-05-15 = 1` was set. The audit team's
Round 1 P20 fix added a bool return, traceback sidecar, and persistent
flag — but the underlying `try_close()` function silently ignores
force-close signals when price is between SL and TP. This is the
single most common case at market close.

**Bug location:** `paper_trading/executor.py:104-113`

```python
reason = None
if current_price <= pos["stop_loss"]:  reason = "stop_loss"
elif current_price >= pos["target"]:    reason = "target"
elif signal == "SELL":                  reason = "signal"

if reason is None:
    return None       # ← "force_close_eod" hits this branch
```

`_force_close_all` (engine.py:286) calls `try_close(sym, price, "force_close_eod")`.
Since `"force_close_eod" != "SELL"` and the price typically sits between SL/TP
at market close, `reason` stays `None` and `try_close` returns silently. The
loop in `_force_close_all` skips the close (`if result:` is False) and continues.
After the loop, `_force_close_all` returns `True` (line 289) — wrongly claiming
success. The main loop then sets `forced_closed_<date> = 1` and the position
stays open.

**Live evidence (May 15, 15:15 IST):**
- TATACOMM.NS — last bar ₹1,679.10, SL ₹1,667.85, TP ₹1,694.05 → between stops → silently skipped
- SAIL.NS — last bar ₹192.35, SL ₹191.89, TP ₹193.54 → between stops → silently skipped
- SYNGENE.NS — last bar ₹454.05, SL ₹452.73, TP ₹456.05 → between stops → silently skipped

All 3 manually force-closed by ops side post-market at the same prices, tagged
`exit_reason="manual_force_close_p20"`. Combined drag: -₹447.85.

**Why the Round 1 audit didn't catch this:** the audit team added bool return
and traceback wrapping around `_force_close_all` — both good hygiene — but
they tested the fix against a synthetic scenario where stops were hit. The
common "between stops" case at force-close time was never exercised. This is
exactly why `AUDIT_ROUND_2_BRIEF.md` mandates **regression-test-before-fix**:
a 5-line test simulating a force-close on a position with price between SL
and TP would have caught this immediately.

**One-line fix applied:**

```python
# paper_trading/executor.py:try_close, after the SELL check
elif isinstance(signal, str) and signal.startswith("force_close"):
    reason = signal       # preserves "force_close_eod" as exit_reason
```

After this fix, `_force_close_all` will actually close positions during the
15:15 window regardless of current price.

**Regression test (the audit team should add this in Round 2):**
Create an open position with price strictly between SL and TP. Call
`try_close(sym, mid_price, "force_close_eod")`. Assert the return is not
None AND the position is removed from `paper_positions` AND a row is
inserted into `paper_trades` with `exit_reason="force_close_eod"`.

**Severity:** Critical. Was the *actual* cause of yesterday's and today's
"positions stuck open over weekend" episodes. The audit team's Round 1
P20 fix was looking at the wrong layer.

---

## P28. No daily safety gates — total exposure, daily loss, daily trade count all uncapped

**Status:** FIXED in Round 3 — test `3c7550d`, fix `3183589`. Three guards now fire in `intraday/engine.py:_process_symbol` BEFORE the BUY-open branch via a new `_p28_daily_gate_block(symbol)` helper. (1) `TOTAL_EXPOSURE_CAP = 0.80` returns `{"_action": "exposure_capped"}` when NSE open equity exceeds 80% of `nse_initial_cash`. (2) `DAILY_LOSS_LIMIT = -0.03` returns `{"_action": "daily_loss_halt"}` when cumulative same-day `paper_trades.net_pnl` drops below -3% of NSE initial. (3) `DAILY_TRADE_CAP = 8` returns `{"_action": "daily_count_capped"}` when same-day closed+open trade count reaches 8. All three counters added to `tick_counts` dict and the per-tick summary log line so a blocked tick is never silent.

**Symptom (observed indirectly May 15):** Engine opened 7 BUYs through Friday. After 4 SL losses in a row (between 11:00 and 15:00 IST), nothing stopped it from continuing to open new positions. Net Day-3 result was -₹708 only because per-trade sizes were small. On a day with bigger sizing (post P25 cap-hit on every trade), the same loss-streak pattern could realize 5-10% NSE drawdown before market close.

**What's enforced today (correctly):**
- Per-trade risk budget: `MAX_RISK_PCT` = 1%
- Per-trade concentration cap: 20% × NSE cash, scaled by remaining slots (P25)
- Per-trade cash safety: ≤ 95% of remaining cash
- Max concurrent positions: `INTRADAY_MAX_POSITIONS` = 5
- Stop-loss on every position: 1 × ATR

**What's NOT enforced (the gap):**

1. **Total simultaneous exposure cap.** With 5 positions at the per-trade cap, ~80% of NSE cash can be deployed at once. No "never more than 70% deployed across all open positions" rule. The 5-slot cap doesn't equal an exposure cap because the per-slot sizes can compound.

2. **Daily loss circuit breaker.** `CLAUDE.md` mentions "Daily loss limit: 3% portfolio" as a design constraint, but **grep of `intraday/engine.py` and `paper_trading/executor.py` finds no enforcement**. It's documentation, not code. A bad day can run unbounded until 15:15 force-close.

3. **Daily trade-count cap.** No upper bound on BUYs-per-day. After 5 SL losses you could still open a 6th if a slot freed up. Backtest cadence is ~1.2 trades/day; production hit 7 on Friday. No alert that flags "you've already traded 5× the backtest cadence today."

**Proposed fix (~30 LOC, three guards in `_process_symbol()` BEFORE the BUY-open branch):**

```python
# In intraday/engine.py near line 210, before "if signal == 'BUY' and pos is None:"

# Guard 1: total simultaneous exposure
from paper_trading.portfolio import get_open_positions, get_market_cash
nse_initial = float(get_config("nse_initial_cash", "500000"))
nse_open_positions = get_open_positions()
if not nse_open_positions.empty:
    nse_open_eq = sum(
        float(r["entry_price"]) * int(r["shares"])
        for _, r in nse_open_positions.iterrows()
        if r["symbol"].endswith(".NS")
    )
else:
    nse_open_eq = 0.0
TOTAL_EXPOSURE_CAP = 0.80  # never deploy > 80% of NSE simultaneously
if nse_open_eq > TOTAL_EXPOSURE_CAP * nse_initial:
    logger.info("%s: total exposure %.0f%% > cap — skipping BUY",
                symbol, (nse_open_eq / nse_initial) * 100)
    return {"_action": "exposure_capped", "symbol": symbol}

# Guard 2: daily loss circuit breaker
import sqlite3
conn = sqlite3.connect("market_data.db")
today_pnl_row = conn.execute(
    "SELECT COALESCE(SUM(net_pnl), 0) FROM paper_trades "
    "WHERE date(exit_time) = date('now','localtime')"
).fetchone()
today_pnl = float(today_pnl_row[0])
DAILY_LOSS_LIMIT = -0.03  # halt new opens if down > 3% NSE
if today_pnl < DAILY_LOSS_LIMIT * nse_initial:
    logger.warning("%s: daily P&L %.2f below -3%% — halting new BUYs",
                   symbol, today_pnl)
    return {"_action": "daily_loss_halt", "symbol": symbol}

# Guard 3: daily trade-count cap
today_count = conn.execute(
    "SELECT COUNT(*) FROM paper_trades "
    "WHERE date(exit_time) = date('now','localtime')"
).fetchone()[0] + len(nse_open_positions)
DAILY_TRADE_CAP = 8  # 5 max open + a handful of closes/re-entries
if today_count >= DAILY_TRADE_CAP:
    logger.info("%s: daily trade count %d >= cap — skipping BUY",
                symbol, today_count)
    return {"_action": "daily_count_capped", "symbol": symbol}
```

**Three new action codes in tick summary:** `exposure_capped`, `daily_loss_halt`, `daily_count_capped`.

**Regression tests (audit team to add):**
1. With 4 open positions whose combined value > 80% of NSE: 5th BUY signal must return `{"_action": "exposure_capped"}` and `paper_positions` count stays at 4.
2. With cumulative day P&L < -3% NSE: next BUY signal must return `{"_action": "daily_loss_halt"}` and no new position opens.
3. With 8 trades already counted today: next BUY signal must return `{"_action": "daily_count_capped"}`.

**Severity:** Medium. Defense-in-depth. Not Monday-blocking (Friday's worst was 0.14% loss), but a real hole that compounds with larger NSE allocation. Worth filing because the gap is silent — engine doesn't tell you the protection isn't there until you have a bad day.

**Acceptance:** all 3 tests pass, engine logs the new action codes when guards fire, no regression in existing tests.

---

## P29. P24 fix is INCOMPLETE — `_load_macro_context` still uses shared SQLite connection (CRITICAL)

**Status:** FIXED in Round 3 — test `a3e2c05`, fix `c85f4e6`. `_load_macro_context` now (a) caches its 4-tuple result behind a process-wide lock with a 5-min TTL so 50-symbol × 8-worker fanout collapses to ONE DB read per tick window instead of ~800, and (b) when the cache misses, opens its own isolated `sqlite3.connect(DB_PATH, check_same_thread=False)` and reads `^NSEI` + `^INDIAVIX` inline (skipping `load_ohlcv`'s call frame entirely). Additionally `data/database.py:get_connection` is now a `@contextmanager` that explicitly closes the connection on exit (Python 3.11's `with sqlite3.connect(...)` only commits, doesn't close — connections were lingering in GC holding shared C state across worker threads). Every worker-reachable read site inherits the per-call connection automatically.

**Symptom (Mon May 18, 09:35–10:00 IST):** Engine crashed **6 times in 30 min** despite Round 2's P24 fix (`2643b42`) being deployed. Exit codes: `0xc0000374 ×3` + `0xc0000005 ×3`. Watchdog kept restarting; each restart triggered another BUY, which triggered another crash.

**Smoking gun from `logs/faulthandler.log` (multiple identical worker-thread stacks):**

```
Thread 0x00005a4c (most recent call first):
  File "pandas/io/sql.py", line 2758 in _fetchall_as_list
  File "pandas/io/sql.py", line 2743 in read_query
  File "pandas/io/sql.py", line 528 in read_sql_query
  File "data/database.py", line 62 in load_ohlcv
  File "features/engineer.py", line 156 in _load_macro_context  ← STILL FIRING
  File "features/engineer.py", line 269 in engineer_features
  File "intraday/engine.py", line 125 in _process_symbol
  File "concurrent/futures/thread.py", line 58 in run
```

**Root cause:** Round 2's P24 fix added per-call `sqlite3.connect()` to `data/database.py:load_ohlcv()` — good. But `features/engineer.py:_load_macro_context` at lines 156 and 159 makes ITS OWN calls to `read_sql_query` or holds onto a shared connection that wasn't fixed. The audit team patched ONE call site (the main OHLCV fetch) but missed the macro-context loader.

`_load_macro_context` runs on EVERY symbol tick, in EVERY worker thread, on every feature engineering call. With 8 workers fanning out, it's the dominant SQLite-race trigger surface — far more frequent than the main OHLCV fetch path that Round 2 fixed.

**Required fix (audit team):**

1. Open `features/engineer.py` at line ~156 and 159. Identify whatever DB read path is used by `_load_macro_context`.
2. Apply the SAME per-call connection pattern Round 2 used in `data/database.py`:
   - Open a fresh `sqlite3.connect(DB_PATH, check_same_thread=False)` inside the function
   - Do the read
   - Close it in a `finally` clause
3. Audit ALL other `read_sql_query` / `pd.read_sql` call sites in the codebase — grep `grep -rn "read_sql" .` and verify every single one uses the per-call pattern.

**This is a process-discipline failure, not just a code defect.** Round 2's regression test `tests/test_p24_buy_path.py::test_concurrent_load_ohlcv_no_crash` only exercised the `load_ohlcv` path. It didn't exercise `_load_macro_context` separately. The test passed because the fixed call site was the only one tested. Round 3's test must cover EVERY thread-entry into SQLite.

**Required regression test (must FAIL on parent commit, PASS on fix):**

```python
# tests/test_p29_macro_context_thread_safe.py
def test_concurrent_load_macro_context_no_crash():
    """Reproduces P29 — N threads loading macro context concurrently."""
    from features.engineer import _load_macro_context
    from concurrent.futures import ThreadPoolExecutor
    def call_it():
        return _load_macro_context()
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: call_it(), range(32)))
    assert all(r is not None for r in results)
```

**Live impact (May 18):** Crash loop ran from 09:35 to 10:00 IST. Engine + watchdog disabled by ops at 10:05 IST. 5 open positions manually force-closed at last 5-min bar prices, tagged `exit_reason="manual_force_close_p24_crash_loop"`. Day-5 P&L: **-₹1,381 net** (₹1,427 from AMBUJACEM double-stop + ₹46 from forced closures).

**Severity:** CRITICAL. Engine is functionally unusable until this lands. Scheduled tasks remain DISABLED until Round 3 ships and verifies.

**Acceptance gate:** Beyond passing the regression test, run the engine for 30 min during off-market hours (which still exercises `_load_macro_context` on every tick). `logs/faulthandler.log` must NOT grow. `Get-EventLog -LogName Application -Source "Application Error"` must show NO new `0xc0000374` or `0xc0000005` events for `python.exe`.

---

## P30. SL cooldown wipes on every engine restart → same symbol re-entered after stop-loss (HIGH)

**Status:** FIXED in Round 3 — test `6d03942`, fix `a66c900`. SL cooldown is now persisted to `paper_config` under key `sl_cooldown_<YYYY-MM-DD>` (mirrors the P20 `forced_closed_<date>` pattern). `_add_to_sl_cooldown(symbol)` updates both the in-memory set and the DB key in one call; `_load_sl_cooldown_for_today()` reads the persisted CSV back into a set. `run_intraday_session()` rehydrates the in-memory set from DB on every startup (including watchdog restarts), so the AMBUJACEM double-stop scenario cannot recur within the same trading day.

**Symptom (Mon May 18, 09:35–10:00 IST):** AMBUJACEM.NS opened, hit stop-loss, was added to in-memory `_sl_cooldown` set. Engine crashed (P29). Watchdog restarted. New process has FRESH (empty) `_sl_cooldown`. Engine immediately re-opened AMBUJACEM.NS at the next BUY signal. **Stopped out again.** Two identical losses on the same symbol within ~10 minutes: -₹712 + -₹715 = **-₹1,427**.

**Root cause:** `intraday/engine.py:34` declares `_sl_cooldown: set = set()` as a module-level Python set. It's pure in-memory state. The cooldown is correctly populated on SL exit (`engine.py:140`) but NOT persisted anywhere. Every engine restart starts with an empty set — including watchdog-driven restarts during the same trading session.

**Interaction with P24/P29:** This bug is dormant in a stable engine because the engine doesn't crash during a session. P29's crash loop activates it — every 5 minutes the cooldown gets wiped, opening the door to re-entry on every recently-stopped symbol. The two bugs compound.

**Required fix:** persist the cooldown to `paper_config` keyed by date, the same way P20 persisted `forced_closed_<date>`:

```python
# intraday/engine.py — at module level
_SL_COOLDOWN_KEY_FMT = "sl_cooldown_{date}"

def _load_sl_cooldown_for_today() -> set:
    """Load today's cooldown from paper_config — survives restarts."""
    from datetime import date
    from paper_trading.portfolio import get_config
    key = _SL_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    raw = get_config(key, "")
    return set(s for s in raw.split(",") if s)

def _add_to_sl_cooldown(symbol: str):
    """Add a symbol to the cooldown AND persist."""
    from datetime import date
    from paper_trading.portfolio import set_config, get_config
    _sl_cooldown.add(symbol)
    key = _SL_COOLDOWN_KEY_FMT.format(date=date.today().isoformat())
    existing = get_config(key, "")
    symbols = set(s for s in existing.split(",") if s)
    symbols.add(symbol)
    set_config(key, ",".join(sorted(symbols)))
```

Then in `run_intraday_session()`, replace `_sl_cooldown.clear()` with `_sl_cooldown.update(_load_sl_cooldown_for_today())` — so a restart inherits the same-day cooldown set.

**Required regression test:**

```python
# tests/test_p30_sl_cooldown_persistence.py
def test_sl_cooldown_survives_restart():
    from intraday.engine import _add_to_sl_cooldown, _load_sl_cooldown_for_today
    _add_to_sl_cooldown("RELIANCE.NS")
    # Simulate restart by clearing in-memory set
    from intraday.engine import _sl_cooldown
    _sl_cooldown.clear()
    # Restart logic should reload from paper_config
    reloaded = _load_sl_cooldown_for_today()
    assert "RELIANCE.NS" in reloaded
```

**Acceptance gate:** After fix, kill engine mid-session with an SL'd symbol in cooldown. Restart engine. Confirm `_sl_cooldown` is repopulated from `paper_config` and that symbol cannot be re-entered.

**Severity:** HIGH. Caused -₹715 (the second AMBUJACEM loss) today. Will fire again on any future crash-loop scenario. Less critical than P29 (which causes the crashes) but they should ship together.

---

## P31. `curl_cffi` shutdown race in `__del__` — access violation on ThreadPoolExecutor exit (LOW)

**Symptom (Mon May 18, 11:00:33 IST):** Engine crashed with `0xc0000005` access violation **during shutdown**, not on a BUY. Captured by faulthandler in `logs/faulthandler.log` lines 645–687.

**Stack trace (paraphrased — all 8 worker threads showed identical frames):**

```
Windows fatal exception: access violation

8 threads simultaneously in:
  File "curl_cffi/curl.py", line 610 in close
  File "curl_cffi/curl.py", line 261 in __del__

Main thread:
  File "threading.py", line 1139 in _wait_for_tstate_lock
  File "threading.py", line 1119 in join
  File "concurrent/futures/thread.py", line 235 in shutdown
  File "concurrent/futures/_base.py", line 647 in __exit__
  File "intraday/engine.py", line 562 in run_intraday_session
```

**What this is:** `curl_cffi` is yfinance's libcurl backend. When `ThreadPoolExecutor.__exit__` triggers shutdown, Python GC fires `__del__` on each worker thread's curl handle simultaneously. `curl_cffi.curl.close()` is not thread-safe under concurrent multi-thread cleanup — heap state collides.

**Not P29.** P29 was SQLite race on BUY path. P31 is curl_cffi race on SHUTDOWN path. Different libraries, different paths, different triggers.

**Reproducibility:** Only on engine shutdown / restart, not on normal ticks. PID 16184 has been alive 29+ min post-fix with no recurrence — confirms this is purely shutdown-path.

**Why low priority:**
- Doesn't fire mid-day during normal operation
- Watchdog absorbs the rare restart case (10s downtime acceptable)
- Engine has graceful exit path 99% of the time (force-close at 15:30 IST); this race only triggers on messy shutdown
- May 18 11:00:33 occurrence was during the chaotic morning transition (disabled→enabled); not normal scenario

**Proposed fixes (in order of effort):**

1. **Cheapest:** Try `curl_cffi` version pin / upgrade. Check pyproject for installed version (`pip show curl_cffi`); see if newer release fixes the close-from-multiple-threads race. Pin in `requirements.txt`.

2. **Explicit session-close before shutdown:** Hold yfinance session refs in each worker; explicitly close them before `ThreadPoolExecutor.__exit__`. Yfinance doesn't expose sessions cleanly — medium effort.

3. **Process-level isolation:** Move yfinance calls to subprocess per fetch. Heavyweight; adds 100-300ms per call. Not worth it for a shutdown-only bug.

4. **Defer entirely:** Phase 3 replaces yfinance with broker direct feed (Upstox / Zerodha Kite) — this bug disappears at that point. Mark as "wontfix-until-phase-3".

**Recommended:** option 1 if a fix exists upstream, else option 4.

**Regression test:** Hard to unit-test — fires only on shutdown. Recommend `scripts/p31_shutdown_stress.py` that spins up + shuts down a ThreadPoolExecutor doing yfinance fetches 50 times; assert no process death. Flaky by nature.

**Severity:** LOW. Not blocking. Polish-for-Phase-3 item.

---

## P32. Windows watchdog scheduled task cadence drifted from 5-min to ~40-min (LOW-MEDIUM)

**Symptom (Mon May 18, 12:45 → 13:24 IST):** `NSE_Engine_Watchdog` task last fired at 12:45:01 IST. Next scheduled fire at 13:25:00 IST per `schtasks /Query` — **40-minute gap instead of the expected 5-min cadence**.

**Task state at observation time:**

```
schtasks /Query /TN "NSE_Engine_Watchdog" /V /FO LIST
  Status:               Ready
  Scheduled Task State: Enabled
  Last Run Time:        18-05-2026 12:45:01
  Next Run Time:        18-05-2026 13:25:00     ← +40 min, not +5 min
```

**watchdog.log confirms the gap:** entries every ~5 min from earliest fire through 12:45, then complete silence.

**Suspected cause:** when ops re-enabled the task today at ~10:58 IST (via `schtasks /Change /ENABLE`), Windows Task Scheduler may have applied the trigger differently than the original creation. The original `Schedule Type: One Time Only, Minute` description suggests a trigger with `RepetitionInterval=PT5M` and `RepetitionDuration=PT24H` (or similar). When the task was disabled this morning, the in-progress repetition cycle may have been lost; re-enable may not have restored it cleanly.

**Why it matters:**
- Watchdog is the safety net that catches engine crashes and triggers restart within 5 min
- With a 40-min cadence, a crash at minute 6 of a window means the engine is dead for 34 min before recovery
- Engine could miss multiple trade signals + skip the 15:15 force-close window entirely
- Today is fine because engine hasn't crashed — but this defeats the whole point of watchdog protection

**Why low-medium not high:**
- Engine has been crash-free since Round 3 deploy (2h 19m as of 13:24)
- Round 3 fixes (P29) should make crashes rare going forward
- Ops side has 15-min independent monitoring sweeps that backstop the watchdog gap

**Required investigation / fix (ops side, not audit team — this is scheduled-task config, not code):**

1. Inspect the task's XML definition via `schtasks /Query /TN "NSE_Engine_Watchdog" /XML > watchdog_task.xml` — look for `<Triggers>` block and verify `<TimeTrigger>` has proper `<Repetition>` with 5-min interval AND `<StopAtDurationEnd>false</StopAtDurationEnd>`.

2. If trigger config is wrong, recreate the task via `schtasks /Create` (delete old + recreate with correct XML) rather than mutating with `/Change`. The XML-based create is more deterministic than enable/disable cycling.

3. Add a daily sanity check: ops monitoring should log a WARNING if `watchdog.log` hasn't been written in > 10 min during market hours.

**Acceptance:** After fix, `watchdog.log` should grow by exactly one entry every 5 min during market hours, with no gaps > 6 min.

**Severity:** LOW-MEDIUM. Engine is currently stable so impact is academic. If engine starts crashing again, this becomes HIGH because recovery time blows up.

---

## P33. SHAP TreeExplainer + xgboost.Booster.predict() race in BUY path (CRITICAL — hot-patched by ops)

**Status:** FIXED in `f590bb1` — SHAP disabled in production. Three lock variants all crashed (cf. commit message for diagnostic chain). xgboost+shap on this stack is fundamentally not thread-safe; further pursuit deferred to P37 below. TreeExplainer RSS measured at 45.8 MB per instance (recorded by `tests/stress/conftest.py:pytest_sessionstart`).

**Original status (May 19):** HOT-PATCHED by ops in commit `89b7eaf` (one `threading.Lock` around the SHAP call). Audit team should formalize with a real fix in Round 4 (per-thread cache or subprocess isolation).

**Symptom (Tue May 19, 09:15:12 + 09:20:32 IST):** Engine crashed **twice in 5 minutes** on Round-3 code that had been stable through all of Monday afternoon. Exit codes: `0xc0000005` (access violation) + `0xc0000374` (heap corruption) — both within 4 sec of a BUY trade firing.

**Smoking gun from `logs/faulthandler.log` (~5 worker threads showing identical frames):**

```
Windows fatal exception: code 0xc0000374

Thread 0x00006494 (representative — 4 more identical):
  File "xgboost/core.py", line 2716 in predict
  File "shap/explainers/_tree.py", line 599 in shap_values
  File "signals/generator.py", line 35 in _shap_reasons
  File "signals/generator.py", line 90 in generate_signal
  File "intraday/engine.py", line 352 in _process_symbol
  [ThreadPoolExecutor worker]
```

**Root cause:** the module-level `_EXPLAINER_CACHE` in `signals/generator.py` returns the SAME `shap.TreeExplainer` instance to every worker. `explainer.shap_values()` internally calls `xgboost.Booster.predict()` which **is not thread-safe**. When 5–8 workers fan out on a tick where multiple symbols want BUYs, they all hit the cached explainer simultaneously, corrupt xgboost's C-level prediction state, and the OS kills the process.

**This was flagged in `AUDIT_ROUND_2_BRIEF.md` as Suspect #1** for the P24 investigation, but the audit team's Round 3 fix only addressed the SQLite race (`_load_macro_context`). The SHAP race survived Round 3 unaddressed — and showed up the moment a market session had multiple high-confidence BUYs in the same tick.

**Why it didn't fire all of Monday afternoon:** Monday post-Round-3 had only ~3 BUYs total (NCC, plus a few others). They never coincided on the same tick. Tuesday morning fired 5+ BUYs across the first two ticks — the SHAP cache got hammered, race triggered.

**Hot-patch applied (commit `89b7eaf`):**

```python
# signals/generator.py
import threading
_SHAP_LOCK = threading.Lock()

def _shap_reasons(ensemble, df_row, signal, top_n=3):
    ...
    explainer = _get_explainer(ensemble.signal_layer.model)
    with _SHAP_LOCK:
        shap_values = explainer.shap_values(df_row[feature_cols])
    ...
```

**Cost:** ~50–200ms per BUY because only one worker runs SHAP at a time. With ~1-3 BUYs per tick, the serialization cost is negligible vs the alternative (engine dying).

**Proper fixes for Round 4 (audit team — pick one):**

1. **Per-thread explainer cache** — `threading.local()` storing a TreeExplainer per worker. Eliminates contention entirely.

   ```python
   _LOCAL = threading.local()
   def _get_explainer(model):
       if not hasattr(_LOCAL, 'cache'):
           _LOCAL.cache = {}
       if id(model) not in _LOCAL.cache:
           _LOCAL.cache[id(model)] = shap.TreeExplainer(model)
       return _LOCAL.cache[id(model)]
   ```

   ~10 LOC, no serialization cost. Memory cost: ~50 MB × 8 workers = 400 MB additional RAM. Tolerable on 8 GB.

2. **Subprocess isolation** — run `generate_signal` in `multiprocessing.Process`. Heavyweight (~300ms startup per call) but bulletproof.

3. **Skip SHAP for high-confidence trades** — if confidence > 0.80, skip the SHAP explanation entirely (use a generic reason string). Sidesteps the issue for the trades you care about most.

**Required regression test (audit team in Round 4):**

```python
# tests/test_p33_shap_thread_safe.py
def test_concurrent_shap_reasons_no_crash():
    """Reproduces P33 — N threads calling _shap_reasons concurrently must not crash."""
    from signals.generator import _shap_reasons
    from models.ensemble import load_ensemble
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor
    
    ensemble = load_ensemble("models/saved/ensemble_intraday.pkl")
    # Build a fixture df_row with all FEATURE_COLUMNS
    df_row = ...
    
    def call_it(_):
        return _shap_reasons(ensemble, df_row, "BUY")
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(call_it, range(32)))
    assert all(isinstance(r, list) for r in results)
```

Test must FAIL on parent of `89b7eaf` (no lock) and PASS on `89b7eaf` (with lock).

**Severity:** CRITICAL. Was actively breaking the engine until the hot-patch landed.

**Live impact (May 19):**
- Engine crashed 2× before the patch (09:15:12, 09:20:32)
- After hot-patch deploy (89b7eaf pushed ~09:30 IST) and engine restart, expect zero further P33 crashes
- One winning trade got through anyway: TECHM.NS +₹1,003.54 at target

---

## P34. Missing test layers — no stress tests, no replay tests (HIGH — root cause of repeated production-discovery)

**Status:** FIXED in `6531cd4` — `tests/stress/` directory built with conftest (RSS measurement) + 5 concurrent test files (SHAP, xgboost, SQLite, yfinance, telegram). pytest count went from 42 baseline → 45 passed + 2 skipped (xgboost-direct gated as P37 future work). The conftest's `pytest_sessionstart` prints live TreeExplainer RSS at every test run so the audit team can grep `PENDING_AUDIT_FIXES.md` for "TreeExplainer RSS" without re-measuring.

**The meta-problem this round.** The reason P33 (SHAP race) survived 3 audit rounds and only surfaced in live Tuesday-morning trading is that **we don't have any test layer that exercises concurrent C-extension calls under realistic load**. Same gap caused P29 to be misdiagnosed as P9 in Rounds 1-2. Same gap will cause Round 4's next-bug-after-P33 to also slip through.

The 42 existing tests in `tests/` are all sequential unit tests. They check "does this function return the right value?" — not "does this function survive 8 workers calling it simultaneously 100 times in a row?"

**What's missing — two distinct test directories:**

### Directory 1: `tests/stress/` — concurrent C-extension stress tests

For every code path that calls a C-extension (xgboost, SHAP, SQLite via pandas, urllib SSL, curl_cffi via yfinance), add a test that runs that path from 8+ worker threads in a tight loop. If the C-extension has a thread-safety bug, the stress test will surface it within 100 iterations.

Required coverage (one test per call site):
- `tests/stress/test_shap_concurrent.py` — replays P33 with `_shap_reasons` under 8 workers
- `tests/stress/test_xgboost_predict_concurrent.py` — direct `ensemble.signal_layer.predict` under 8 workers
- `tests/stress/test_sqlite_concurrent.py` — replays P29 (already exists as `test_p24_buy_path.py`, move + rename)
- `tests/stress/test_yfinance_concurrent.py` — concurrent `yf.Ticker.history` calls (P31 territory)
- `tests/stress/test_telegram_concurrent.py` — concurrent `send_message` (touches urllib SSL)

Each test: ThreadPoolExecutor(max_workers=8), iterate 100×, assert process survives. Should fail-loudly on heap corruption / access violation by checking `Get-EventLog` for new entries after the run (Windows-specific guard).

### Directory 2: `tests/replay/` — actual market-data replay

Save 1 day of fully-recorded ticks + bars + signals + trades to a fixture file (~50 MB). Build a replay harness that feeds those ticks into the engine in-process and asserts the exact same trades fire with the same prices and the same exits.

Required fixtures:
- `tests/replay/fixtures/2026-05-18.tar.gz` — Monday's clean day (cleanest reference)
- `tests/replay/fixtures/2026-05-19_morning.tar.gz` — Tuesday morning's chaos (regression test for the crash loop — should now run clean post-P33 fix)

Test asserts:
- Same number of BUYs fire
- Same exit prices on each closed trade
- Final `nse_cash` matches the fixture's recorded value
- Zero new entries in Windows Event Log after the run

**Effort estimate:** ~2-3 days total. Stress dir is ~1 day (per-callsite ~1-2 hr × 5 sites). Replay dir is ~1.5 days (fixture builder + harness + 2 fixtures).

**Cost-benefit:** every bug caught here costs ~₹0 (weekend coding time). Same bug caught in live trading costs ~₹500-2,000 (today's P33 already cost ~₹1,000+ in messy trades). Pays for itself within the first ~3 bugs caught.

**Why this is HIGH priority not LOW:**
- Round 4 will fix P33 properly (per-thread cache or subprocess). Without P34's stress tests, **Round 4's fix is unverifiable** the same way Round 3's was. We'd be re-running the same play: "fix lands, looks green, ships, blows up on the first multi-BUY tick."
- P34 + P33 should ship as a bundle: write the failing stress test first, then the proper fix, then verify the test passes. Same discipline AUDIT_ROUND_2_BRIEF.md mandated for regression tests.

**Severity:** HIGH — meta-bug. Every future round will keep missing the same class of bug until this lands.

**Acceptance:** All 5 stress tests + at least 1 replay test exist and pass. Audit team confirms the new tests are wired into the pre-push verification gate.

---

## P37. Restore SHAP explainability (deferred from P33) — needs subprocess isolation or upstream fix

**Background:** P33 was "fixed" in `f590bb1` by **disabling SHAP entirely** in production. The strategy is unaffected (SHAP reasons were cosmetic display only), but BUY/SELL alerts now show a generic `"Model confidence based on pattern ensemble"` message instead of the per-feature breakdown like `"rsi = 55.2 (bullish), macd = 14.5 (bullish), nifty_return = 0.012 (bullish)"`.

**What was tried in Round 4 (all failed):**

1. **Module cache + lock around `shap_values()` only** (May 19 hot-patch `89b7eaf`): construction-race crash on cache-miss.
2. **Per-thread `TreeExplainer` cache via `threading.local()`**: all explainers wrap the same underlying `xgboost.Booster`; `Booster.feature_names` / `predict` race at C-level.
3. **Module cache + double-checked lock around BOTH construction and `shap_values()`**: still crashes inside `xgboost/data.py:712 _from_pandas_df` — the booster's C-state mutates during data prep even when the SHAP layer is locked.

**Root cause:** `shap.TreeExplainer` + `xgboost.Booster` on this exact stack (Python 3.11.8, xgboost 3.2.0, shap latest, Windows) is **fundamentally not thread-safe at any lock granularity short of "1 thread total"**. This is an upstream issue, not a codebase-patchable bug.

**Restoration paths (audit team — pick one when convenient, not urgent):**

1. **Subprocess isolation** — run SHAP in a `multiprocessing.Process` per BUY. ~30 lines in `signals/generator.py`. Cost: ~300ms startup per call (versus 50-200ms serialization with the locks that don't work). Bulletproof — separate Python interpreter, no shared C state. Likely the right answer.

2. **Upstream version bump** — try Python 3.12 + xgboost 4.x + shap latest combination on a fresh laptop. If a known-thread-safe combo exists, pin in `requirements.txt`. Free if it works.

3. **Single-threaded SHAP path** — refactor the engine so SHAP runs on the main thread (sequentially after worker fan-out finishes). Heavy refactor; not worth the complexity for cosmetic display.

4. **Don't restore.** Keep the generic message. The strategy doesn't use SHAP; the user reads alerts on a phone where compact alerts are arguably better.

**Why not urgent:**
- Strategy unaffected
- Alerts still functional (just less informative)
- Dashboard SHAP column shows generic message gracefully (no broken UI)

**Stress test ready for revival work:** `tests/stress/test_xgboost_predict_concurrent.py` has 2 tests permanently skipped with explicit markers pointing at this entry. The `tests/stress/test_shap_concurrent.py` will need its gate updated when SHAP comes back online.

**Severity:** Low. Feature regression, not a correctness or stability bug.

---

## Notes for the audit team

- Production today is running on the user's Windows laptop (8 GB RAM,
  Python 3.13, xgboost 3.2.0). Models load fine, no crashes.
- VPS (Linux, Python 3.9, xgboost 2.1.4) is scheduled paused — its
  cron + systemd timer for intraday are disabled to prevent double
  trading. Will be re-enabled once xgboost version question is
  resolved (Python 3.11 upgrade vs. retraining on 2.1.4).
- All fixes from the May 13 audit (Q1–Q7) are deployed and running.
- The retrained `ensemble_intraday.pkl` is the active model on laptop.
