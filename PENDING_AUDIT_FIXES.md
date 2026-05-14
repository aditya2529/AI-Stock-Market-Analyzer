# Pending Audit Items — For Next Audit Round

Living document of issues surfaced after the first audit (May 13, 2026)
that need the scrum team's attention in a follow-up audit. Each entry
includes evidence and a proposed minimal fix.

---

## P1. Position sizer is not cash-aware (single trade eats 70% of capital)

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
