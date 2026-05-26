# R8 — Upstox v3 historical adapter — ops handoff

**Status:** ready for ops review. All commits local; no push.
**Branch:** `master`
**Commits:** `decc380` (RED tests) → `f0b0986` (instruments cache) →
`78f5a08` (v3 + rate limiter) → `ca4ec38` (CLI + dispatch + schema fix) →
`785898b` (sandbox integration + adapter shape fix).

---

## What ships

A production-grade Upstox v3 historical-candle fetcher with:

- Symbol-mapping cache (auto-downloads Upstox master CSV, 7-day TTL)
- Token-bucket rate limiter enforcing all three Upstox limits
  concurrently (25/sec, 250/min, 1000/30 min)
- Date-range chunking (5-day windows; Upstox v3 rejects longer)
- yfinance-compatible return shape (drop-in for the existing pipeline)
- New opt-in CLI flag: `python main.py fetch --source upstox`
- Ingestion-layer routing — 5m + `--source upstox` automatically uses
  the v3 endpoint; 1d + `--source upstox` keeps using the v2 endpoint
- The existing P42 25-symbol static dict in `live_trading/symbol_map.py`
  is UNTOUCHED — R8's dynamic 200-symbol lookup lives in a separate
  module so the live-order path keeps its deterministic mapping

## New CLI flag

```bash
# Default — bit-for-bit identical to pre-R8 behaviour
python main.py fetch --market nse --resolution 1d --years 3

# NEW — opt into Upstox v3 for >60-day 5m backfill
python main.py fetch --market nse --resolution 5m --years 2 --source upstox
python main.py fetch --symbol RELIANCE.NS --resolution 5m --years 2 --source upstox
```

- `--source` choices: `yfinance` (default) | `upstox`
- Default value is `yfinance`, so any pre-existing operator workflow
  (cron jobs, scripts/run_monthly_retrain.bat, scripts/deploy_us_session.sh)
  continues to behave exactly as before unless `--source upstox` is
  added explicitly.
- 5m + `--source upstox` automatically routes to the v3
  `/historical-candle/{key}/minutes/5/...` endpoint.
- 1d + `--source upstox` keeps using the existing v2 day/week/month
  endpoint (the P42 contract).

## .env requirements

The Upstox adapter reuses the existing P42 env vars — no new keys.

```
# Required for --source upstox (any of sandbox or prod)
UPSTOX_ENV=sandbox            # or "prod"
UPSTOX_SANDBOX_API_KEY=...
UPSTOX_SANDBOX_API_SECRET=...
UPSTOX_SANDBOX_ACCESS_TOKEN=...
# OR
UPSTOX_PROD_API_KEY=...
UPSTOX_PROD_API_SECRET=...
UPSTOX_PROD_ACCESS_TOKEN=...
```

**Sandbox vs prod for historical fetches:** Both return REAL exchange
data (per Upstox 2026-05-24 addendum: only order endpoints are
sandbox-simulated; data endpoints hit the real exchange feed
regardless of which token authenticated). Choose sandbox for
cost-free integration testing; choose prod for the full 200-symbol
backfill (run by ops, not auto-triggered).

## Rate-limit behaviour

The adapter uses a token-bucket triad that enforces all three Upstox
limits concurrently. Once any bucket fills, the adapter sleeps just
long enough for the oldest entry to expire, then proceeds.

| Bucket | Cap | Window |
|---|---:|---|
| Per second | 25 req | 1 sec |
| Per minute | 250 req | 60 sec |
| Per 30-min | 1000 req | 1800 sec |

For the 200-symbol × 2-year backfill:
- 200 symbols × ~73 chunks/symbol (1 year ÷ 5-day chunks ≈ 73)
- ≈ **14,600 requests total**
- At 25 req/sec ceiling, ≈ 10 min of wall-clock
- Per-30-min cap (1000) bites at the 1000-req mark — adapter
  auto-pauses for ~12 min, then resumes
- Total wall-clock for full backfill: **~45-60 min** end-to-end

## Cache file (Upstox instrument master)

- Location: `data/cache/upstox_instruments.csv`
- Source: `https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz`
- TTL: 7 days (re-downloaded on first lookup after mtime expires)
- Size: ~25 MB uncompressed (~5 MB on the wire)
- Format: CSV (NOT parquet — keeps requirements.txt unchanged)
- Schema we use: `instrument_key`, `exchange`, `tradingsymbol`,
  `instrument_type`. The full master has more columns; we ignore them
- Filter: `instrument_type == "EQUITY"` — excludes ~7k bond +
  government-securities rows that would otherwise alias to `.NS`
  tickers the engine never trades

## How to run the full 200-symbol backfill (ops, NOT auto-triggered)

```bash
# Sanity check first — single symbol, last 30 days
UPSTOX_INTEGRATION_TEST=1 python -m pytest \
    tests/test_r8_sandbox_integration.py -v

# Full backfill (run when ready — ~45-60 min wall-clock)
python main.py fetch --market nse --resolution 5m --years 2 --source upstox
```

The full backfill uses ALL three rate-limit buckets to completion.
Expect the adapter to auto-pause periodically — that's normal, not
a hang.

## Rollback

If something breaks after a future engine boot:

1. **Revert the R8 commit series:**
   `git revert 785898b ca4ec38 78f5a08 f0b0986 decc380` (in that order)
2. yfinance keeps working as before; live engine is uninterrupted.
3. The cache file at `data/cache/upstox_instruments.csv` can be deleted
   safely — it's only read by the (now-reverted) adapter code path.

## Test coverage

Unit tests (run on every `pytest`):
- `tests/test_r8_upstox_instruments.py` — 8 cases for the cache loader
- `tests/test_r8_upstox_historical.py` — 11 cases for v3 method +
  token-bucket triad
- `tests/test_r8_cli_source_flag.py` — 11 cases for CLI flag + ingestion
  routing + P42 regression guards

Integration tests (run with `UPSTOX_INTEGRATION_TEST=1`):
- `tests/test_r8_sandbox_integration.py` — 3 cases against real
  Upstox sandbox; verifies schema parity with yfinance + end-to-end
  DB write

Total at this commit: **206 passed, 2 skipped** unit-only;
**209 passed, 2 skipped** with integration gate enabled.

## Files touched (R8)

```
data/adapters/upstox_adapter.py        (extended — v3 method + rate limiter)
data/adapters/upstox_instruments.py    (NEW — master CSV cache)
data/ingestion.py                       (extended — `source` kwarg + dispatch)
main.py                                  (extended — `--source` arg)
tests/test_r8_upstox_instruments.py     (NEW)
tests/test_r8_upstox_historical.py      (NEW)
tests/test_r8_cli_source_flag.py        (NEW)
tests/test_r8_sandbox_integration.py    (NEW, network-gated)
R8_UPSTOX_HANDOFF.md                    (NEW — this doc)
```

**Zero touches to:** `live_trading/` (P42 path), `paper_trading/`,
`alerts/`, `signals/`, `backtesting/`, `api/`, `dashboard/`, `scripts/`,
`models/`, `data/database.py` schema, `data/validator.py`, `config.py`,
`.env`, `requirements.txt`, `PENDING_AUDIT_FIXES.md`, the existing
177 pre-R8 pytest cases, the 25-symbol P42 static dict in
`live_trading/symbol_map.py`.

## Known quirks (worth flagging for the ops sign-off review)

1. **Upstox v3 URL ordering:** `to_date` precedes `from_date` in the
   path (e.g. `/minutes/5/2026-04-30/2026-04-26`). This is consistent
   with the existing v2 endpoint that the adapter already uses — the
   reversal is on the Upstox side.

2. **Chunk size 5 days:** empirically determined against the sandbox.
   Upstox rejects longer windows with `UDAPI1148: Invalid date range`.
   If a future Upstox update widens that, edit the constant
   `_V3_INTRADAY_CHUNK_DAYS` in `data/adapters/upstox_adapter.py`.

3. **Master CSV has ~9k NSE_EQ rows but only ~2k are equities.** The
   rest are bonds + SDL series + government securities — none of
   which the engine trades. The EQUITY filter in `_load_lookup_table`
   excludes them. Future Upstox schema changes that drop the
   `instrument_type` column would break this filter; the schema
   check in `_cache_is_fresh` catches that explicitly and forces a
   re-download (which would still fail until the loader is updated).

4. **Process-local rate limiter:** the `UpstoxRateLimiter` lives at
   class-level on `UpstoxAdapter`, so a single python process honors
   all three limits across the full backfill. A multi-process
   backfill (e.g. via `multiprocessing.Pool`) would NOT share the
   buckets and could exceed the cumulative caps. Out of scope today;
   if needed, swap to an IPC-backed limiter (Redis token bucket or
   sqlite locking).
