# Audit Round 4 — Critical Test-Infrastructure + P33 Proper Fix

**Drafted:** May 19, 2026 (Tue evening) · **Repo HEAD:** `f042ab4` · **Deadline:** Wednesday May 20, 06:00 IST

Engine + watchdog are ENABLED and will auto-boot Wed 09:10 IST. If you don't ship clean by Wed 06:00, ops will disable tasks and Wednesday is lost.

---

## Since you were last engaged (Round 3 — Sunday)

Round 3's P29 fix held Monday afternoon (4h 40m, 0 crashes) but Tuesday morning exposed a NEW bug. Ops side handled urgent items today; **do NOT redo them**:

**Already shipped by ops today (don't touch, don't re-fix):**
- **P33 hot-patch** (commit `89b7eaf`) — `threading.Lock` around `_shap_reasons`, keeping the engine alive but serializing SHAP calls. WORKS but band-aid.
- **P35** (commits `d81c71a` + `48c8e29`) — added `confidence` + `regime` columns to `paper_trades` + dashboard trade history.
- **P36** (commit `0d97df3`) — REMOVED NYSE entirely from API + `paper_config`. Reconciled `nse_cash` to ₹4,97,687.26 (real). Deleted duplicate TECHM trade row (race condition between worker threads closing same position).
- **Mobile-friendly CSS** (commit `f042ab4`) — dashboard now works on phones.

**Verified chain from rollback target → current HEAD (6 commits):**
```
89b7eaf  audit-fix: P33 hot-patch (lock)
1941dfd  audit-pending: P33 entry
40211a5  audit-pending: P34 entry
d81c71a  ops: P35 — confidence + regime columns
48c8e29  ops: P35 — schema fix
0d97df3  ops: P36 — NYSE removal + reconciliation
f042ab4  ops: dashboard mobile CSS
```

**Current metrics (post-cleanup, real numbers):**
- 31 lifetime trades, 14 wins / 17 losses, **WR 45.2%, PF 0.84**
- Lifetime P&L: **-₹2,312.74**
- NSE cash: **₹4,97,687.26**
- Peak: **₹5,00,000** (reset to baseline post-cleanup)
- 0 open positions

---

## What you need to read first

1. `PENDING_AUDIT_FIXES.md` entries **P33, P34** (mandatory) + **P31, P32** (Tier 2)
2. `logs/faulthandler.log` lines ~690 onward for the P33 stack trace
3. `signals/generator.py` — see the current `threading.Lock` band-aid in `_shap_reasons` that you're replacing

---

## Tier 1 — MUST land. Bundled, in this order.

### Item 1 — P34: build the stress-test infrastructure FIRST

Yes, tests before fix. Without P34, P33's proper fix would ship the same way Round 3 did — looks green, blows up under load.

**Build `tests/stress/` directory:**

- `tests/stress/__init__.py` — empty
- `tests/stress/conftest.py` — shared fixtures (load ensemble pkl, build sample df_row)

**`[FIX #2]` While building conftest:** please measure and log the actual TreeExplainer RSS cost via `os.getpid()` + `tasklist` (or equivalent) on this codebase's ensemble pkl. The "~50 MB per explainer" estimate in Item 2 is unverified — ops side tried to measure but Python on this box has neither `resource` nor `psutil`, and ctypes psapi returned 0. Record the real number in a comment at the top of `test_shap_concurrent.py`. If actual > 150 MB per explainer, flip Item 2 to the lock-based approach and document why.

**`[FIX #4]` Also:** when you commit the FIXED status update for P33 in `PENDING_AUDIT_FIXES.md`, include the measured number on the same line:

```
**Status:** FIXED in <sha>. Per-thread cache: TreeExplainer RSS
measured at X MB on current ensemble_intraday.pkl; 8-worker total
= ~8X MB additional.
```

That makes it queryable next round (grep `PENDING_AUDIT_FIXES.md` for `TreeExplainer RSS`) without re-measuring. The test-file comment stays too, just don't make it the only record.

**`tests/stress/test_shap_concurrent.py`:**

```python
def test_shap_reasons_8_workers_no_crash():
    """8 workers calling _shap_reasons in parallel must survive 200 calls."""
    from signals.generator import _shap_reasons
    from concurrent.futures import ThreadPoolExecutor
    ensemble = load_test_ensemble()
    df_row = make_test_row()
    def call_it(_):
        return _shap_reasons(ensemble, df_row, "BUY")
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(call_it, range(200)))
    assert all(isinstance(r, list) for r in results)
```

**Other required files:**

- `tests/stress/test_xgboost_predict_concurrent.py` — Direct `ensemble.signal_layer.predict` under 8 workers, 200 calls.
- `tests/stress/test_sqlite_concurrent.py` — Move + rename existing `tests/test_p24_buy_path.py` here.
- `tests/stress/test_yfinance_concurrent.py` — Concurrent `yf.Ticker.history` calls — covers P31 surface.
- `tests/stress/test_telegram_concurrent.py` — Concurrent `telegram_bot.send_message` — urllib SSL race surface.

Each test: `ThreadPoolExecutor(max_workers=8)`, 200 iterations. ~150 LOC total.

### Item 2 — P33: replace the lock band-aid with per-thread cache

Current (commit `89b7eaf`): module-level `threading.Lock` serializes SHAP. Cost ~50-200ms per BUY.

Proper fix: per-thread explainer cache via `threading.local()`. No contention.

In `signals/generator.py`, replace `_EXPLAINER_CACHE` + `_get_explainer` + `_SHAP_LOCK` with:

```python
import threading
_LOCAL = threading.local()

def _get_explainer(model):
    if not hasattr(_LOCAL, 'cache'):
        _LOCAL.cache = {}
    key = id(model)
    if key not in _LOCAL.cache:
        _LOCAL.cache[key] = shap.TreeExplainer(model)
    return _LOCAL.cache[key]
```

Then remove the `with _SHAP_LOCK:` wrapper in `_shap_reasons`.

Each worker gets its own TreeExplainer — no shared state, no race, no serialization cost.

**`[FIX #2]` Memory cost:** estimate ~50 MB per explainer × 8 workers = ~400 MB. **UNVERIFIED** — depends on measurement from P34 conftest above. If actual per-explainer cost > 150 MB, fall back to the lock-based approach and document why in the fix commit message. The 8 GB laptop has ~5 GB free during market hours; absolute ceiling is ~3 GB additional before paging becomes painful.

**CRITICAL:** `test_shap_reasons_8_workers_no_crash` MUST PASS on this fix.

### Commit sequence

1. Commit P34 conftest + helpers (with measured RSS comment)
2. Commit P34 stress test files (pass against current lock-based hot-patch — expected)
3. Commit P33 proper fix (per-thread cache, removes lock)
4. `pytest tests/ -q` must show **47+ passed**
5. `PENDING_AUDIT_FIXES.md` status updates: P33 + P34 FIXED (including the RSS measurement line per FIX #4)

---

## Tier 2 — if Tier 1 lands before 02:00 IST

### Item 3 — P31: curl_cffi shutdown race

Try `pip show curl_cffi`, upgrade if newer release exists, pin in `requirements.txt`.

### Item 4 — P32: watchdog scheduled task XML recreate

```
schtasks /Query /TN "NSE_Engine_Watchdog" /XML > /tmp/wd.xml
```

Inspect, `/Delete`, `/Create` fresh with explicit `/SC MINUTE /MO 5`. Verify next fire happens 5 min later.

---

## Out of scope — DO NOT touch

- P2, P4, P6, P13, P14, P15, P17, P19, P22, P30 — closed or low priority
- ALREADY-SHIPPED today: P33-hotpatch (`89b7eaf`), P35, P36, mobile CSS
- Anything in `models/` or `signals/generator.py` beyond the P33 replacement
- `config.py` constants (`MAX_RISK_PCT`, `INTRADAY_MAX_POSITIONS`, `BROKERAGE_PCT`, `SLIPPAGE_PCT`)
- Anything in `dashboard/` — ops side owns
- `paper_trades` rows on master — audit trail
- `paper_config` — just reconciled post-NYSE-removal, leave alone
- `AUDIT_ROUND_*.md` briefs — historical, leave intact

---

## Verification gates — all must clear before each push

1. `python -m pytest tests/ -q` — **47+ passed** (42 baseline + 5 new stress)

2. `python main.py review` — must run without smoke

3. Each new stress test, run 5× in a row — all 5 must pass (flaky tests worse than missing)

4. **`[FIX #3]` 15-min off-market engine run** after Tier 1 — both must hold:
   - (a) `logs/faulthandler.log` byte length must not grow
   - (b) `Get-WinEvent` (preferred on Win11) shows zero `python.exe` crashes:

     ```powershell
     $crashes = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=(Get-Date).AddMinutes(-20)} -ErrorAction SilentlyContinue | Where-Object { $_.Message -like '*python.exe*' }
     if ($crashes) { Write-Error "FAIL: $($crashes.Count) crashes"; exit 1 } else { Write-Output "PASS" }
     ```

     Legacy fallback if you prefer: `Get-EventLog -LogName Application -Source "Application Error" -After (Get-Date).AddMinutes(-20) -ErrorAction SilentlyContinue | Where-Object { $_.Message -like '*python.exe*' }` — works but deprecated in Win11.

---

## Commit hygiene — non-negotiable

- Test commits FIRST, fix commits SECOND
- One commit per item, prefix: `audit-fix: P# — short reason`
- For each closed item: `**Status:** FIXED in <sha>` line under entry in `PENDING_AUDIT_FIXES.md`
- Final summary commit on clean tree before Wed 06:00 IST
- DO NOT bundle P33 with P34 in one commit (lose bisect)

---

## If stuck

Push a STUCK comment under the relevant P# entry:

```
**STUCK (audit team, <timestamp>):** What I tried: X. What happened: Y. What I expected: Z. Need: <ask>.
```

Don't grind past 03:00 IST. Tier 1 only > half-finished everything.

**Common stuck scenarios:**
- "stress test passes without my fix." → Test isn't reproducing. Bump workers to 16, iterations to 500.
- "Per-thread cache uses too much RAM." → Per Item 2 fallback: revert to lock-based, document why.

---

## `[FIX #1]` Rollback plan — explicit blast radius

If anything breaks worse than current:
- **Rollback target:** `89b7eaf` (P33 lock baseline — known stable)
- `git revert <bad-sha>` and push (don't force-push)
- Add `WED: ROLLED BACK, DO NOT REMOVE LOCK` note at top of `PENDING_AUDIT_FIXES.md`
- Ops disables scheduled tasks for Wednesday

**What you lose by rolling back to `89b7eaf` (be aware before pulling the trigger):**
- P35 (confidence + regime columns in trade history + dashboard)
- P36 (NYSE removal + nse_cash reconciliation + dup TECHM deletion)
- Mobile-friendly dashboard CSS

All four are SAFE to roll back — they're additive ops-side changes, no engine logic depends on them. The cleanup-database-state changes in P36 cannot be re-undone by git revert (DB is not git-tracked) — `paper_config` is already in the post-P36 state and stays there.

**Only roll back if Tier 1 ships AND breaks the engine.** Don't roll back for the dashboard / accounting cleanup — those are good code.

---

## Wednesday 08:45 IST ops sanity check

1. `git log --oneline -15` — Round 4 audit-fix commits present, properly prefixed
2. `python -m pytest tests/ -q` — 47+ passing
3. `python main.py review` — clean
4. `grep -n "threading.Lock\|_SHAP_LOCK\|threading.local" signals/generator.py` — confirm per-thread cache replaced lock
5. `paper_positions` empty
6. `logs/faulthandler.log` byte length unchanged since your final push
7. `python -c "from intraday.engine import run_intraday_session"` imports clean

If all 7 pass: tasks stay enabled, engine boots Wed 09:10. Live test.
If any fail: tasks disabled, Round 5 brief generated.

---

## What success looks like by Wed 06:00 IST

- `tests/stress/` has 5 concurrent test files
- pytest: 47+ passing (42 baseline + 5 new)
- `signals/generator.py` uses `threading.local()` instead of `threading.Lock`
- `PENDING_AUDIT_FIXES.md`: P33 + P34 marked FIXED (with TreeExplainer RSS number per FIX #4)
- 15-min off-market run: zero crashes (`Get-WinEvent` gate clean)
- Engine module imports cleanly

Tier 2 done if time: P31 + P32 also marked FIXED.

---

## The bigger point

Rounds 1-3 each shipped a fix that "looked green" against unit tests, then blew up on first multi-BUY tick. The pattern: NO test exercises concurrent C-extension calls. P34 is the meta-fix — adding the layer of safety that lets future rounds catch races BEFORE production.

If you only have time for ONE item this round, ship P34. The lock-based P33 hot-patch is already keeping the engine alive — losing P33 proper fix costs 50-200ms per BUY but doesn't crash. Missing P34 guarantees a Round 5 surprise within a week.

Ship clean. Discipline > speed.
