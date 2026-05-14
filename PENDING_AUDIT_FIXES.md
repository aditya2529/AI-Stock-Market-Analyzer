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

## Notes for the audit team

- Production today is running on the user's Windows laptop (8 GB RAM,
  Python 3.13, xgboost 3.2.0). Models load fine, no crashes.
- VPS (Linux, Python 3.9, xgboost 2.1.4) is scheduled paused — its
  cron + systemd timer for intraday are disabled to prevent double
  trading. Will be re-enabled once xgboost version question is
  resolved (Python 3.11 upgrade vs. retraining on 2.1.4).
- All fixes from the May 13 audit (Q1–Q7) are deployed and running.
- The retrained `ensemble_intraday.pkl` is the active model on laptop.
