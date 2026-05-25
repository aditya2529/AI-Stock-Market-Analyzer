# LIVE_TRADING_RUNBOOK — operator procedures for the P42 demo path

**Audience:** ops (the human who runs the demo trade).
**Status:** for the single-trade ₹500–₹2,000 demo path. Do not extend
this runbook for multi-position or auto-trading — those are explicit
out-of-scope items per the P42 brief.

---

## 1. Pre-flight (ops sign-off checklist)

Before flipping `LIVE_TRADING=true` for ANY environment:

- [ ] `python -m pytest tests/ -q` shows green (160+ passed, 2 skipped)
- [ ] `python main.py live status` runs without errors against sandbox creds
- [ ] One end-to-end sandbox demo + sandbox close completed successfully
- [ ] Paper engine ran one full clean session post-merge (PID stable,
      watchdog quiet, alerts on time, force-close auto, zero new
      `logs/faulthandler.log` entries)
- [ ] Telegram fired on the sandbox order (verify the `[LIVE-SANDBOX]`
      tag prefix in your phone alert)
- [ ] Watchdog still catches a synthetic crash (kill the engine PID
      mid-session, confirm watchdog restart fires within 5 min)

---

## 2. Daily Upstox token re-login (~09:00 IST on demo day)

Upstox v2 access tokens expire daily at ~03:30 IST. To run a demo on
any given day:

1. Open https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=YOUR_API_KEY&redirect_uri=YOUR_REDIRECT_URI
   in a browser. Replace `YOUR_API_KEY` and `YOUR_REDIRECT_URI` with
   the matching env's values from your `.env`.
2. Complete the Upstox OAuth flow. The browser redirects to
   `http://127.0.0.1:8000/upstox/callback?code=...`.
3. Exchange the `code` for an access_token via Upstox's
   `/v2/login/authorization/token` endpoint (curl/Postman, one-shot).
4. Paste the returned `access_token` into the matching slot in `.env`:
   - For sandbox testing: `UPSTOX_SANDBOX_ACCESS_TOKEN=<paste>`
   - For the prod demo trade: `UPSTOX_PROD_ACCESS_TOKEN=<paste>`
5. **Restart your shell / Python session.** The `.env` is re-read on
   every kill-switch call, but the upstox_client's session-cached
   profile data lives in process memory and won't reload until the
   process restarts.

---

## 3. Flipping the kill switch

`LIVE_TRADING` is the master gate. Default OFF.

### Enable the live path

1. Edit `.env`.
2. Change `LIVE_TRADING=false` to `LIVE_TRADING=true`.
3. Save. (No restart needed — `dotenv_values` re-reads on every call.)
4. Verify: `python main.py live status` should show
   `kill switch: ✓ enabled + all active-env creds present`.

### Disable the live path (ROLLBACK)

1. Edit `.env`.
2. Change `LIVE_TRADING=true` back to `LIVE_TRADING=false`.
3. Save. The very next live API call refuses with
   `RuntimeError: LIVE_TRADING disabled`.

No deploy needed. This is the entire rollback procedure for any P42 issue.

---

## 4. Flipping between sandbox and prod

`UPSTOX_ENV` selects WHICH credential set authenticates.

### Switch to sandbox (for testing)

1. Edit `.env`.
2. Set `UPSTOX_ENV=sandbox`.
3. Save + restart any active `live` CLI session (so cached profile
   refreshes against the sandbox identity).
4. Verify: `python main.py live status` shows `UPSTOX_ENV: 'sandbox'`.

### Switch to prod (for the actual demo)

1. Confirm sandbox path was tested end-to-end today.
2. Edit `.env`.
3. Set `UPSTOX_ENV=prod`.
4. Confirm `UPSTOX_PROD_ACCESS_TOKEN` is freshly populated from the
   daily re-login.
5. Save + restart shell.
6. Run `python main.py live status`. Visually confirm `UPSTOX_ENV: 'prod'`
   AND `kill switch: ✓ enabled + all active-env creds present`.

**Identity check before any prod order:** Run `python main.py live demo
--symbol RELIANCE.NS --qty 1`. The CONFIRM preview will show
`identity: <YOUR REAL NAME> (<YOUR USER ID>)`. If the name does not
match the account you intend to trade in — abort with Ctrl+C **BEFORE
typing CONFIRM**, then verify which token set was loaded.

---

## 5. Placing a demo order

```
python main.py live demo --symbol RELIANCE.NS --qty 1
```

Optional flags:
- `--limit-price 1450.00` — switches to LIMIT order at the given price.
  Without this, the order is MARKET.

Flow:
1. Pre-flight safety checks (kill switch, symbol map, LTP cap, slot cap)
   — refuses with a clear error message before any side-effect happens.
2. Preview banner shows env, identity, symbol, qty, LTP, estimated cost.
3. Prompt: `Type CONFIRM to place order (anything else aborts):`
4. Type EXACTLY `CONFIRM` (case-sensitive, no quotes, no spaces). Any
   other input (including `confirm`, `yes`, `y`) aborts the flow.
5. Order placed via Upstox `/v2/order/place`. CLI polls for fill
   status up to 5 seconds.
6. Result banner shows `order_id`, `fill_price`, and one of:
   - `✓ ORDER FILLED`
   - `⏳ ORDER PLACED — FILL PENDING`
   - `⚠ ORDER FILLED — POST-FILL CAP BREACH` (if MARKET slippage took
     the actual notional over ₹2,000)
7. Telegram alert fires with `[LIVE-{ENV}]` prefix.

### What if the order doesn't fill within 5 seconds?

You'll see `[FILL PENDING]` in the Telegram alert AND in the CLI output.
This is rare for liquid NSE equities — usually MARKET orders fill in
<2 seconds. To check the actual fill status, open the Upstox web app
or mobile app and look at the order book. The trade row in
`live_trades.db` has `upstox_fill_price=NULL` until you manually
verify and update.

---

## 6. Closing the position

```
python main.py live close --symbol RELIANCE.NS
```

Flow:
1. Kill-switch check.
2. Loads the open row from `live_trades.db`.
3. Preview banner: original side/qty/entry + warning that an
   opposite-MARKET will fire.
4. CONFIRM gate (same EXACTLY-`CONFIRM` semantics as `demo`).
5. Opposite-side MARKET order placed + 5s fill poll.
6. Original `live_trades.db` row marked `CLOSED` with `exit_price`.
7. Telegram alert.

The brief allows only one open position at a time. If you have no open
position, `live close` exits 1 with `no open live position to close`.

---

## 7. Status check (read-only, anytime)

```
python main.py live status
```

Makes ZERO API calls. Reads `.env`, `live_trades.db`, and
`logs/live_trading.log`. Safe to run when `LIVE_TRADING=false`.

Shows:
- Current `LIVE_TRADING` + `UPSTOX_ENV` values
- Kill switch evaluation (✓ / ✗ with reason)
- Open positions count + each row's symbol/side/qty/entry/fill/order_id
- Last 5 events from the audit log

Useful for: "wait, what state am I in?" — particularly after a flag flip.

---

## 8. Aborting mid-flow

- **Before CONFIRM** — `Ctrl+C` is safe. No order has been placed.
  The preview is just a read of Upstox data; nothing was committed
  to `live_trades.db` and no Telegram alert has fired yet.
- **After CONFIRM, before fill poll completes** — `Ctrl+C` interrupts
  the CLI but the order is already in Upstox's queue. The post-fill
  steps (`live_trades.db` write, post-fill notional check, Telegram
  alert) will not run. Open the Upstox app to check the order status,
  then either let it fill normally or cancel it in the app. If the
  order filled, manually re-run `python main.py live close --symbol X`
  later to square off.
- **After the result banner prints** — the flow has completed. Use
  `live close` to square off.

---

## 9. Audit log

`logs/live_trading.log` — JSON-lines, append-only, one event per line.

```
tail -n 20 logs/live_trading.log
grep '"action": "place"' logs/live_trading.log
grep '"order_id": "ORD-XYZ"' logs/live_trading.log | jq .
```

Fields (canonical):
- `ts` — ISO 8601 UTC timestamp
- `action` — `preview` | `place` | `poll` | `fill` | `fill_pending` |
  `record` | `reject` | `close_start` | `close_complete`
- `env` — `sandbox` | `prod`
- `symbol`, `qty`, `price`, `ltp`, `fill_price`, `fill_status`,
  `order_id`, `original_order_id`, `close_order_id`
- `user_confirmed` — `true` only on `place` and `close_start`
- `error` — populated on `reject`
- `user_name`, `user_id` — recorded at `preview` time for the
  paste-the-wrong-token forensic trail

---

## 10. Common errors + meaning

| Message | Meaning | Fix |
|---|---|---|
| `LIVE_TRADING disabled (value='false')` | Kill switch is off | Set `LIVE_TRADING=true` in .env, save, retry. |
| `UPSTOX_ENV invalid: 'production'` | Env name typo | Must be exactly `sandbox` or `prod` (lowercase). |
| `UPSTOX_SANDBOX_API_KEY missing or empty` | Active-env cred slot empty | Re-paste the key into .env. |
| `unknown symbol 'XYZ.NS'` | Not in symbol_map | Add the yfinance→Upstox ISIN mapping to `live_trading/symbol_map.py`. |
| `notional 2500.0 > MAX_LIVE_NOTIONAL 2000` | qty × price over ₹2,000 cap | Lower qty or wait for price to drop. |
| `position slot full: 1 open >= MAX_LIVE_POSITIONS 1` | Already have an open trade | `live close --symbol <existing>` first. |
| `HTTP 401: Unauthorized` | Daily token expired | Re-login per section 2. |
| `[FILL PENDING]` Telegram | Poll exhausted; order may still fill | Check Upstox app; close manually once filled. |
| `[⚠ CAP BREACH]` Telegram | Post-fill notional > ₹2,000 | Order already filled; no auto-cancel. Decide whether to close immediately or hold. |

---

## 11. Rollback (single-line)

```
# In .env:
LIVE_TRADING=false
```

Save. Every live code path is disabled instantly. The paper engine,
dashboard, watchdog, and Telegram paper alerts continue unaffected
because they don't import from `live_trading/`.

---

## 12. What this runbook does NOT cover

- Auto-trading (model → live order without human CONFIRM) — explicitly
  out of scope.
- Multi-position live trading — capped at 1 position by `kill_switch.MAX_LIVE_POSITIONS`.
- Bracket / cover orders / SL-M / GTT — out of scope per the brief.
- Shorting — the project is long-only.
- Refresh-token automation — daily manual re-login is the v1 path.
- Background fill-status poll past the 5s foreground window — degraded
  behavior is `[FILL PENDING]` Telegram + manual broker check. If
  this becomes painful, file P43.
