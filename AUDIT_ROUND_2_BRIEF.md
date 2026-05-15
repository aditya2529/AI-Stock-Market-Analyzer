# Audit Round 2 — Weekend Action Brief
**Drafted:** May 15, 2026 (Fri evening) · **Repo HEAD:** `56f280c` · **Deadline:** Monday May 18, 09:00 IST

---

## To the audit team

Read this whole brief before touching anything. It's longer than Round 1's because Round 1 taught us that compressed briefs lead to fixes-that-don't-fix. This time we're giving you the full picture — context, evidence, sequencing, gates, and escape hatches. You should never feel stuck or in the dark; if you are, the answer is in here or in the file paths I've pointed you at. If after reading this AND the evidence you still feel stuck, **stop and message back** rather than guessing.

The goal is simple: **Monday morning's 09:10 IST auto-boot must run cleanly through 15:30 IST close without a single P9-class crash, without a single position stuck open at 15:15, and without an alert lost.** We don't need every audit item closed. We need the things that broke on Friday to be unbreakable on Monday.

---

## Where we are right now

**What Round 1 fixed (correctly):** P1 (per-market cash for sizing), P3 (confidence default 0.60), P20 plumbing (bool return, traceback sidecar, persistent flag), P21 (entry-side fees in cash flow). These all held under live load on Friday.

**What Round 1 fixed but didn't actually solve:**
- **P9/P11** — you fixed the UTF-8 hygiene path. The real root cause is something else entirely (see P24 below). Engine still crashed 6 times Friday. Not your fault — we (ops) misdiagnosed it as Unicode and you trusted the diagnosis. Lesson learned. This round, **verify the failure first, fix second**.
- **P20** — your plumbing was perfect, but the underlying `try_close()` function silently rejected the `force_close_eod` signal when price was between SL and TP. Three positions sat open over the weekend because of this. Ops side already shipped the one-line fix as commit `a34497b` (P26). Your job: add the regression test that should have caught it in Round 1.

**What ops side handled in the meantime:**
- Added `faulthandler.enable()` to `main.py` (commit `3095b38`) — the next P24 crash dumps a real C-stack. We have one captured now in `logs/faulthandler.log`. Read it.
- Manually force-closed 3 stuck positions at last 5-min bar prices, tagged `exit_reason="manual_force_close_p20"`. Reconciled `nse_cash` to ₹5,00,671.66.
- Added a dashboard `/api/signals/{symbol}` lookup UI for ad-hoc signal checks (commit `56f280c`). Tailscale-accessible from phone.

**What broke on Friday that you must fix this weekend:**
- P24: engine dies ~6 sec after every BUY (6 crashes Friday, 4 on Thursday)
- P25: slot-aware sizing — the `remaining_slots` block from your original P1 proposal got dropped
- Regression test gap — Round 1 had none; that's why P9/P11 and P20 both shipped half-broken

---

## Friday trading recap (so you understand the stakes)

- Day 3, net **-₹708** (3 winners at target +₹1,685; 4 SL losses -₹2,393)
- 7 trades closed via the strategy + 3 stuck-and-manually-closed (-₹448)
- 12 trades lifetime, 12/30 toward the strategy-decision gate
- Strategy itself is performing within backtest band (43% win rate vs 47% backtest)
- The pain is operational: crashes, late alerts, stuck positions — not the model

---

## Tier 1 — these MUST land before Monday open

These are non-negotiable. If you can only do one thing this weekend, do P24.

### Item 1: P24 — heap corruption on every BUY (CRITICAL)

**What you're looking at**

`logs/faulthandler.log` already contains a captured C-stack from Friday's 15:00:07 crash. Open it now and read all of it. You'll see this pattern repeated across worker threads:

```
Thread A (and 5 more identical):
  pandas/io/sql.py:2758 _fetchall_as_list
  data/database.py:62 load_ohlcv
  features/engineer.py:156 _load_macro_context
  features/engineer.py:269 engineer_features
  intraday/engine.py:125 _process_symbol
  [thread pool worker]

Current thread (the one that died):
  ssl.py:580 _load_windows_store_certs       ← crash point
  ssl.py:596 load_default_certs
  ssl.py:775 create_default_context
  urllib/request.py do_open
  alerts/telegram_bot.py:30 send_message
  alerts/telegram_bot.py:144 send_signal_alert
  alerts/dispatcher.py:37 on_signal
  intraday/engine.py:250 _process_symbol  (BUY-opened branch)
```

**Two separate races, both real:**

1. **Windows SSL cert store race** — multiple worker threads simultaneously call `urllib.request.urlopen()` (one for the BUY alert, others for trade-closed alerts). Each invocation triggers `_load_windows_store_certs()` which is not thread-safe on Windows. The Python issue tracker has multiple reports of heap corruption from concurrent SSL context creation on Windows.

2. **SQLite + pandas race** — multiple workers call `load_ohlcv()` → `pd.read_sql_query()` → fetches from a SQLite connection that isn't safe to share across threads. SQLite by default uses `check_same_thread=True` but pandas may bypass this. Even if it doesn't, the connection itself isn't designed for concurrent reads.

Either race alone can corrupt the process heap. With 8 workers fanning out, both fire frequently. The audit team's earlier Q5 fix bumped `max_workers` from 2 to 8, which made this much worse.

**The surgical fix (don't just drop max_workers to 1 — that costs you 50× per-tick latency)**

Two changes, both small, both verifiable:

**Fix A — SSL cert store (preload once at module load):**

In `alerts/telegram_bot.py`, at the top after imports:
```python
import ssl
# P24: Windows SSL cert store loading is not thread-safe. Preload the
# default context ONCE at import time, before any worker threads exist,
# so worker threads only reuse the already-loaded context rather than
# racing on _load_windows_store_certs.
_SSL_CONTEXT = ssl.create_default_context()
```

Then in `send_message()`, pass `context=_SSL_CONTEXT` to `urllib.request.urlopen()`:
```python
with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as resp:
    ...
```

**Fix B — SQLite thread-local connections:**

In `data/database.py`, change `get_connection()` (or wherever the shared connection lives) so that each thread that calls `load_ohlcv` opens its own short-lived SQLite connection:
```python
def load_ohlcv(symbol, lookback_days=...):
    conn = sqlite3.connect(DB_PATH)   # fresh per call — cheap
    try:
        return pd.read_sql_query("SELECT ... WHERE symbol = ?", conn, params=(symbol,))
    finally:
        conn.close()
```

Don't try to use connection pooling for SQLite — it's not worth it. The connection-open overhead on a local file is sub-millisecond.

**Regression test you MUST write before pushing the fix**

Create `tests/test_p24_buy_path.py`:
```python
import threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

def test_concurrent_telegram_sends_no_crash():
    """Reproduces P24 — N threads calling urlopen simultaneously should not crash."""
    from alerts.telegram_bot import _SSL_CONTEXT
    def send():
        # Use a real but harmless HTTPS endpoint
        req = urllib.request.Request("https://api.telegram.org/")
        try:
            urllib.request.urlopen(req, timeout=5, context=_SSL_CONTEXT).read()
        except Exception:
            pass  # we don't care about HTTP errors — only process survival
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: send(), range(16)))
    # If we get here without process death, fix works.

def test_concurrent_load_ohlcv_no_crash():
    """Reproduces P24's SQLite race — N threads loading data concurrently."""
    from data.database import load_ohlcv
    symbols = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS"] * 4
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(load_ohlcv, symbols))
    assert all(r is not None for r in results)
```

**This test must FAIL on parent commit `56f280c` and PASS on your fix commit.** Commit the test FIRST in its own commit, then the fix in a second commit. That's bisect-friendly.

**Acceptance gate (don't skip)**

After pushing the fix, run the engine for at least 30 minutes against live yfinance data (you can do this during off-market hours — the BUY path still exercises the same code). Confirm:
- `logs/faulthandler.log` does NOT grow during the run
- `Get-EventLog -LogName Application -Source "Application Error"` shows NO new `python.exe` `0xc0000374` or `0xc0000005` events
- `python -m pytest tests/ -q` is green

If any of these fail, ROLL BACK. Don't ship a partial fix.

**If Fix A + B don't fully resolve it**

Fall back path (in order):
1. Drop `max_workers=8` → `max_workers=4` in `intraday/engine.py:344`. If crashes stop → there's a third race we haven't found.
2. Wrap `send_message()` in a `threading.Lock` shared across the module — serializes all Telegram sends. Loses some parallelism but eliminates the SSL race deterministically.
3. Move Telegram sending to a separate process via `multiprocessing.Process` — fully isolated. Heavyweight but bulletproof.

If you go beyond Fix A+B, **document why in the commit message** and update P24's entry in `PENDING_AUDIT_FIXES.md`.

---

### Item 2: P25 — slot-aware position sizing (HIGH)

**The gap**

Your Round 1 P1 fix changed `executor.py:_position_size()` to use per-market cash. Great. But the *original* proposal you drafted (in `PENDING_AUDIT_FIXES.md`'s P1 entry) also included `remaining_slots` accounting that you dropped from the shipped fix. Today's data shows the gap is latent — risk-based sizing usually shields it — but a day of 5 tight-SL signals would deploy ~95% of NSE cash with zero buffer.

**The fix (your own original proposal, finished)**

In `paper_trading/executor.py:_position_size()`:
```python
def _position_size(entry_price, stop_loss, portfolio_value, symbol=None, max_positions=5):
    from config import MAX_RISK_PCT
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return 0
    
    # Risk-budget gate (existing)
    risk_amount = portfolio_value * MAX_RISK_PCT
    shares_by_risk = int(risk_amount / risk_per_share)
    
    # Slot-aware concentration cap (NEW)
    if symbol is not None:
        from paper_trading.portfolio import get_open_positions, _market_of
        market_suffix = _market_of(symbol)
        open_positions = get_open_positions()
        if not open_positions.empty:
            open_count = sum(
                1 for _, r in open_positions.iterrows()
                if _market_of(r["symbol"]) == market_suffix
            )
        else:
            open_count = 0
        remaining_slots = max(1, max_positions - open_count)
        # Reserve 80% of (cash / remaining_slots) per trade, leaving 20% buffer
        max_capital_per_trade = (portfolio_value / remaining_slots) * 0.80
    else:
        # Backward compat — old call sites that don't pass symbol
        max_capital_per_trade = portfolio_value * 0.20
    
    shares_by_cap = int(max_capital_per_trade / entry_price)
    return max(0, min(shares_by_risk, shares_by_cap))
```

Update the call site in `paper_trading/executor.py:try_open()` to pass `symbol=symbol`. Don't change `intraday/engine.py:240` — that already passes per-market cash via the P1 fix.

**Regression test** (`tests/test_p25_slot_aware_sizing.py`):
```python
def test_first_trade_at_20pct_when_all_slots_open():
    # With 5 slots and no open positions: each trade caps at 20% of NSE cash
    # (0.80 / 5 = 0.16 effective when sized to leave headroom for all slots)
    ...
def test_fifth_trade_leaves_buffer():
    # With 4 positions already at-cap, the 5th should size to <=80% of remaining
    ...
```

**Acceptance gate**

Simulate 5 BUYs back-to-back at the cap (use a fixture script). After the 5th `try_open()`, `nse_cash` must be ≥ 5% of `nse_initial_cash`. Currently with the old logic it can hit ~0%.

---

### Item 3: Regression test for P26 (force-close fix already shipped)

Ops side fixed P26 in commit `a34497b` (one line in `try_close`). Your job is to write the test that should have caught it in Round 1:

```python
# tests/test_p26_force_close.py
def test_force_close_succeeds_when_price_between_sl_and_tp():
    """Reproduces P26 — try_close must close on force_close_* signals
    even when current price is between SL and TP."""
    from paper_trading.portfolio import open_position, get_position
    from paper_trading.executor import try_close
    
    # Setup: a position with SL=100, TP=120, current price=110 (between)
    open_position("TEST.NS", entry_price=110, shares=10, stop_loss=100, target=120)
    assert get_position("TEST.NS") is not None
    
    result = try_close("TEST.NS", current_price=110, signal="force_close_eod")
    assert result is not None, "force_close must close even between SL/TP"
    assert result["exit_reason"] == "force_close_eod"
    assert get_position("TEST.NS") is None
```

Test FAILS on commit `3095b38` (parent of the fix), PASSES on `a34497b`.

---

## Tier 2 — strongly preferred for Monday, defer only if Tier 1 takes the full weekend

### Item 4: P22 — startup Telegram heartbeat (~15 LOC)

User opened the project Friday 09:00 IST expecting confirmation the engine had booted; nothing arrived until the first BUY at 09:15. Silent boot looks identical to a silent failed boot from the user's phone.

**Fix:** at the top of `run_intraday_session()` after `init_paper_tables()`, send one Telegram message:
```python
from alerts.telegram_bot import send_message
send_message(
    f"🟢 <b>Engine started</b>\n"
    f"<i>{_ist_now().strftime('%a %d %b %H:%M IST')}</i>\n"
    f"NSE cash: ₹{get_market_cash('nse'):,.0f}\n"
    f"Symbols: {len(symbols)}"
)
```
That's it. No regression test needed (it's a one-off send), just verify visually on next boot.

### Item 5: P16 — NYSE cash phantom credit (~₹159K leak)

`paper_config.nyse_cash` shows ₹258,950 but no NYSE trade has ever run. The bump from ₹1L → ₹5L NSE on May 14 likely wrote through the wrong key. Reconcile:
```python
# Validation: cash should equal initial + realized P&L
expected = nse_initial + nyse_initial + sum(paper_trades.net_pnl)
actual = nse_cash + nyse_cash
assert abs(expected - actual) < 1.0
```
Walk back `paper_config` to a known-good state. Add a startup invariant check that logs WARNING if reconciliation fails.

**Be careful** — this touches live state. Take a backup of `market_data.db` before any UPDATE.

---

## Tier 3 — only if Tier 1+2 are done by Sunday evening

- **P13** — Universe scanner ERROR-spam on 3 delisted symbols (TATAMOTORS, LTIM, PEL). Either curate them out of the seed list or downgrade scanner 404s from ERROR to WARNING. 5-min fix.
- **P2** — `max_pos` counter race condition. May be implicitly fixed by P24's thread-safety work. Re-evaluate after P24 lands.

---

## Out of scope — DO NOT TOUCH this weekend

- **`models/`**, **`features/engineer.py`**, **`signals/generator.py`** — except where P24 explicitly leads you into `engineer.py:_load_macro_context` (and there it's the DB call, not the feature logic).
- **`config.py` constants** — `MAX_RISK_PCT`, `INTRADAY_MAX_POSITIONS`, `BROKERAGE_PCT`, `SLIPPAGE_PCT`, confidence thresholds. PRD-level.
- **P6** (dashboard Linux-only health tab), **P14** (info-only baseline), **P15** (verify already fixed, no work).
- **P17**, **P19** — re-evaluate after P24 lands; almost certainly downstream symptoms.
- **P18** — already retracted.
- **The 12 rows in `paper_trades`** — that's our audit trail. Don't modify.
- **`AUDIT_ROUND_1_BRIEF.md`-equivalent in this file's history** — leave the P24/P25 timeline intact for forensics.

---

## Commit hygiene & branching

- One commit per item, `audit-fix: P# — short reason` prefix.
- **Test commits FIRST, fix commits SECOND.** If you bundle them you lose bisect.
- Push to master per standard workflow.
- For each item closed, edit `PENDING_AUDIT_FIXES.md` and add `**Status:** FIXED in <sha>` immediately under the entry's title.
- Final summary commit on a clean tree before Monday 06:00 IST.

---

## Verification gates (every push must clear all of these)

1. `python -m pytest tests/ -q` — must be green (currently 23 passing, will be more after you add P24/P25/P26 tests)
2. `python main.py review` — must complete without smoke
3. For P24 specifically: 30-min live run with zero `0xc0000374` / `0xc0000005` events in Windows Event Log
4. For P25: the slot-aware sizing test passes
5. For P26: the force-close-between-stops test passes
6. `git status` clean before final commit

---

## If you get stuck

Stop and write a comment in `PENDING_AUDIT_FIXES.md` under the item you're stuck on, in this format:
```
**STUCK (audit team, <timestamp>):** What I tried: X. What happened: Y. What I expected: Z. Need: <ask>.
```
Then commit + push that comment, and the ops side will see it and respond. Don't burn the weekend grinding on a dead-end.

Specific likely-stuck scenarios:
- **"My P24 fix didn't stop the crashes."** → Run the `tests/test_p24_buy_path.py` regression test on the parent commit. If it doesn't crash without your fix, the test isn't reproducing the bug — refine it. If it does crash, then your fix is incomplete — fall back to wrapping in a `threading.Lock`.
- **"SQLite refuses connections from multiple threads."** → That means `check_same_thread=True` (the default) is firing. Pass `check_same_thread=False` to `sqlite3.connect()` AND ensure each thread uses its own connection. Both are needed.
- **"I broke an existing test."** → Don't push. Revert your change and reach out. Existing 23 tests are the safety net.

---

## Rollback plan

If anything in Tier 1 lands but causes the engine to behave worse than Friday:
- Rollback target: `a34497b` (after P26 force-close fix but before any Round 2 work)
- Command: `git revert <bad-sha>` and push (don't force-push)
- Notify ops side via a top-of-file note in `PENDING_AUDIT_FIXES.md`: "MONDAY: ROLLED BACK TO a34497b — DO NOT RUN"
- Ops will disable the Monday scheduled task

---

## Monday morning sanity check (ops side will run this at 09:00 IST)

Before the scheduled 09:10 boot, ops will verify on master HEAD:
1. `python -m pytest tests/` green — all old + new tests pass
2. `python main.py review` clean
3. `git log --oneline -10` shows your commits with `audit-fix:` prefix
4. `paper_positions` is empty (0 stuck from weekend)
5. `paper_config.nse_cash` ≈ ₹500,671 (or whatever the reconciled figure is post-Friday)
6. `logs/faulthandler.log` has no fresh content since you last pushed
7. Telegram test ping (`python -c "from alerts.telegram_bot import send_test_ping; print(send_test_ping())"`) returns True

If all 7 pass: ops greenlights the auto-boot. If any fail: ops disables the scheduled task and re-engages you.

---

## What success looks like by Monday 06:00 IST

- `tests/` directory has at least 3 new tests (P24, P25, P26) all passing
- `PENDING_AUDIT_FIXES.md` has `**Status:** FIXED in <sha>` on P22, P24, P25, P26, and ideally P16
- Engine can be started manually, opens a BUY, dispatches Telegram, and survives — no crash
- A 30-min live run shows zero entries in Windows Event Log for `python.exe` crashes
- Force-close playbook proven via the P26 regression test
- Position sizing proven via the P25 regression test
- One `🟢 Engine started` Telegram will arrive on Monday 09:11

That's the bar. We can hit it in a normal weekend's worth of focused work. Don't rush; verify each step.

---

## Closing note

Round 1 was educational. You shipped fixes that addressed real symptoms but missed deeper causes because the briefs were too compressed and the verification was too thin. Round 2's structure exists precisely to prevent that. The faulthandler stack is the evidence we didn't have last week; use it. The regression-tests-first discipline is the safety net we didn't have last week; build it.

If you need me to clarify anything in this brief before you start, that's a fair use of a back-and-forth. Once you're underway, work on it independently and surface only at gates or stuck-points.

You have everything you need. Ship clean code.

—Ops Claude (acting as senior engineering manager for the weekend window)
