# Prompt 23: IB orders and ibguard coordination

Covers SPEC.md §14 (trade mode), §15, §16, §17.4 (the sweeps), §17.6, §19 (the trade-mode confirm, cancel, the ibguard routes, `limits` and `services` in the state document), §20.2 item 7 and §21. Acceptance: AC-26 and AC-27.

## 1. Steps

1. **`btlab/live.py`** (pure) gains the trade-mode line rules: the limits of §15.3 (value, percent of equity, and the count keeping the largest lines; BLOCKED_LIMIT), TW sizes (round down to the size increment, 1,000 when IB gives none; SKIPPED_MIN_SIZE), market-rule rounding (buys down, sells up; SKIPPED_NO_PRICE_RULE), and the trade-mode line states.
2. **`btlab/trading.py`**, which depends only on the `OrderPort` and `AccountPort` protocols and domain dataclasses (`OrderTicket` with IB's field values, `OrderReport`):
   - generation in trade mode: IB positions as the current holdings, ledger cash plus IB quantities as the live equity, contracts (SKIPPED_NO_CONTRACT), sizes, price rules and limits;
   - `send`: the preconditions (connected, the configured or only account, before the cutoff, limits again, IB positions equal to those the batch was built on, else BATCH_STALE), reducing lines first, the §16.2 protocol for each reducing line, then the one send function with its refusals (quantity not above 0, a reduction larger than the position, a btlab order already working on the contract, run mode not trade); tickets with `orderRef = btlab:<batch>:<line>:<UTC ms>` stored on the line before sending;
   - order reports: SENT → WORKING → FILLED, PARTIAL, CANCELLED, REJECTED, EXPIRED; fills with quantity, price and commission on the line; READ_ONLY_API (error 321) records the event, stops the batch and emails; the end of a protected line's order restores or closes its exclusion (PROTECTION_RESTORED);
   - recovery at each connect by exact `orderRef` (UNKNOWN_BTLAB_ORDER for a `btlab:` order that matches no line), cancelling btlab's own working orders of a batch, and the sweep 15 minutes after the session (lines checked against IB's orders, day orders cancelled at the close EXPIRED, ACTIVE exclusions resolved, a failing include retried each minute with one event and one email);
   - the trade-mode ledger: cash from the capital, fills, costs and dividends net of withholding; holdings are IB's quantities; HOLDINGS_MISMATCH when they differ from what the ledger expects.
3. **`btlab/ib_orders.py`**: the ib_async `OrderPort` on the link's connection (`MarketOrder` with `tif="OPG"`, `LimitOrder` with `tif="DAY"`, `outsideRth=False`, `account`, `orderRef`; `placeOrder`, `cancelOrder` of this client's own trades; `openTrades`, `reqCompletedOrdersAsync`, `fills` with commission reports; `orderStatusEvent`). Every name checked in ib_async 2.1.0. It is the only module that calls `placeOrder` or `cancelOrder`.
4. **`btlab/ib_link.py`** gains the `AccountPort` side: the account's positions by conId and the market rules (`reqMarketRuleAsync`).
5. **`btlab/ibguard_client.py`**: login, state, thresholds and exclude (§16.1), with the requester of §5.7; the pure threshold preview (STK positions that would exit at once, long and short, take-profit and stop-loss, unknown prices), its SHA-256 token over the canonical JSON, and the range checks (`tp_pct` in (0, 1000], `sl_pct` in (0, 100)).
6. **Jobs**: LIVE_SEND (on a trade-mode confirmation), LIVE_CANCEL, LIVE_SWEEP (15 minutes after each session's end through the scheduler's tick, at start and after each IB connect), trade-mode LIVE_GENERATE after updates; the emails BATCH_DONE, PROTECTION_RESTORED, IBGUARD_RESTORE_FAILED and READ_ONLY_API.
7. **Wiring** (`__main__.py`): trade mode builds the order adapter on the link's connection and the trader; test mode never constructs either.
8. **Routes**: the trade-mode branch of confirm (409 BATCH_STALE, NOT_CONNECTED, BATCH_EXPIRED; LIMITS_NOT_SET cannot happen since trade mode refuses to start without limits), `POST /api/live/batches/{id}/cancel`, `GET /api/ibguard/preview?backtest_id=` (400 NO_IBGUARD_EXIT, 409 IBGUARD_UNREACHABLE), `POST /api/ibguard/thresholds` (204; 409 PREVIEW_CHANGED or IBGUARD_UNREACHABLE); `services.ibguard` in the state document.
9. **UI**: the Live view's trade-mode parts (the account kind in the dialog, the ibguard exclusions the confirmation will make, [Cancel working orders]) and the ibguard panel (pick a backtest that used the ibguard rule, the preview, a confirmed write); the shell already shows TRADE·paper or TRADE·live with the red banner.
10. **Tests** (AC-26, AC-27): a fake `OrderPort`, a fake `AccountPort`, an aiohttp server that mimics ibguard's API, and a scan of the source tree for `placeOrder`, `cancelOrder` and `reqGlobalCancel`.

### Readings where the spec is silent

- **ibguard's payloads** (its SPEC §11 is not in this repository) are read as: `GET /api/state` → `{"account", "account_kind", "paused", "thresholds": {"STK": {"tp_pct", "sl_pct"}, …}, "positions": [{"source": "REAL", "con_id", "symbol", "sec_type", "currency", "quantity", "ref", "price", "status", "pending_exit"}, …]}`; login answers 2xx with the session cookie. The owner checks these against ibguard's API documentation (checklist below); a mismatch shows as IBGUARD_UNREACHABLE with BAD_PAYLOAD and the field.
- **Order states from IB**: ib_async's `Submitted`/`PreSubmitted` are WORKING; `Filled` FILLED; `Cancelled`, `ApiCancelled` and `Inactive` end the order (PARTIAL with fills, REJECTED with an error, CANCELLED otherwise; the sweep calls an unfilled day order cancelled at the close EXPIRED). ib_async 2.1.0 treats error 321 as a validation warning (`ValidationError`), so the adapter reports it as a rejection with code 321.
- **Lines the batch never sent** after READ_ONLY_API, or after an internal error, are NOT_SENT.
- **The trade-mode ledger row** of a session is written by the first LIVE_GENERATE after its data arrive, when the session's closes and dividends are known.

## 2. Extension points

- **Another order kind** is a ticket field mapping in `ib_orders.py`.
- **Per-market ibguard thresholds** (TODO.md) would change only the preview and write.

## 3. Refactor seams

- `trading.py` sees only `OrderPort`, `AccountPort` and dataclasses; ib_async stays in `ib_link.py` and `ib_orders.py`.
- Limits, sizes and price rules are pure in `live.py`, used at generation and again before sending.

## 4. Deferred items (appended to TODO.md)

- A timing race in an older test (prompt 14's cancelled optimization), seen once while running this prompt's suite.

## Manual checklist: trade mode on the paper account (SPEC.md §24.3 item 7)

Preparation:

1. In IB Gateway, log in to the **paper** account; under API settings uncheck "Read-Only API"; keep the socket port 4002.
2. Start ibguard on its **paper** database and check that it shows the paper account.
3. Check ibguard's API documentation (its SPEC §11) against the payloads assumed above: the state's `account`, `account_kind`, `paused`, `thresholds.STK`, and each position's `source`, `con_id`, `symbol`, `sec_type`, `quantity`, `ref`, `price`, `status`, `pending_exit`.
4. In `config.toml`: `mode = "trade"`; `[limits]` with your own numbers (orders per day per market, percent of equity, value per market); `[ib]` port 4002, client id 31; `[ibguard]` url and token.
5. Start btlab. It refuses to start while a limit is missing or not positive, naming the key. Home shows **TRADE · paper** (no red banner) and IB and ibguard connected.

A small batch:

6. Create a US live portfolio from a daily backtest with a small capital (Live view), then [Regenerate] after the US update.
7. Check the lines: quantities in whole shares, BLOCKED_LIMIT for lines above your limits, limit prices on the tick grid when the order kind is LIMIT.
8. [Confirm] a batch with one or two lines: the dialog shows the paper account and any ibguard exclusions; confirm.
9. In TWS (paper), find the orders: `orderRef` starts with `btlab:`, MOO orders are MKT with TIF OPG (LIMIT orders DAY), outside-RTH off, account set.
10. Check that orders of other client ids or without the `btlab:` prefix are untouched.

A protected sell:

11. Hold a position that ibguard protects (open it in TWS or from an earlier batch) and let a batch sell it; confirm before the open.
12. ibguard's UI shows the position EXCLUDED right after btlab sends the sell.
13. In the Live view, [Cancel working orders] before the open; confirm the dialog.
14. The order is cancelled, ibguard shows the position protected again (included), an event PROTECTION_RESTORED appears and its email arrives.

ibguard thresholds:

15. Open the ibguard panel, pick a backtest that used the ibguard rule, review the preview (account, paused state, current and new STK thresholds, the positions that would exit at once), confirm the write; ibguard shows the new STK thresholds and an event THRESHOLDS_WRITTEN appears.

Afterwards: set `mode = "test"` again, or keep "Read-Only API" checked in IB Gateway whenever Claude Code works on this PC.

## Result

- **`btlab/live.py`**: trade-mode line states (TRADE_SKIPS, ORDER_STATES, ENDED), `apply_limits` (value, share of equity, count keeping the largest) and `rule_price` (buys down, sells up on the market rule's grid).
- **`btlab/trading.py`**: `OrderTicket` (IB's field values), `OrderReport`, the `OrderPort` and `AccountPort` protocols, `TradeError`, and `Trader`: trade-mode lines from IB's positions (contracts, SKIPPED_NO_CONTRACT, SKIPPED_MIN_SIZE, SKIPPED_NO_PRICE_RULE, limits), `precheck` (NOT_CONNECTED, BATCH_EXPIRED, BATCH_STALE), `send` (limits again, reducing lines first, §16.2 for reducing lines, the send function's refusals, `orderRef` stored with the line before the order goes, waiting for IB's first answer), order reports (WORKING, FILLED, PARTIAL, CANCELLED, REJECTED; commissions arriving after a fill still recorded; READ_ONLY_API stopping the batch with its event and email; BATCH_DONE), `recover` by exact orderRef (UNKNOWN_BTLAB_ORDER), `cancel` of btlab's own working orders, `sweep` (day orders cancelled at the close EXPIRED), exclusions restored (PROTECTION_RESTORED) or CLOSED with retried failures (one IBGUARD_RESTORE_FAILED event and email), the trade-mode ledger with HOLDINGS_MISMATCH, `on_connect`, and `stay_connected` (10 s doubling to 60 s).
- **`btlab/ib_orders.py`**: `IbOrders` on ib_async 2.1.0 (`MarketOrder`/`LimitOrder` with `tif`, `outsideRth`, `account`, `orderRef`; `placeOrder`; `cancelOrder` only for this client's `btlab:` orders; `openTrades`, `reqCompletedOrdersAsync`, `orderStatusEvent`, `commissionReportEvent`), and `report()` mapping ib_async's states (321 as a rejection, 202 as a cancellation).
- **`btlab/ib_link.py`**: `account()`, `positions()`, `increments()` and the `on_connect` hook.
- **`btlab/ibguard_client.py`**: login, state, STK thresholds and exclude through the shared requester; `guard_state`; the pure `preview` with its SHA-256 token; `thresholds_error`.
- **Jobs and scheduler**: LIVE_SEND (from a trade-mode confirmation), LIVE_CANCEL, LIVE_SWEEP (`scheduler.due_sweep` 15 minutes after each session, once; every minute while a restore is pending; at start); trade-mode LIVE_GENERATE after updates with the ledger catching up first; trade mode without IB refuses with NOT_CONNECTED; IB errors end jobs as `IB_<kind>`.
- **`__main__.py`**: trade mode builds `IbOrders` on the link's connection, the `Trader`, the connect hook, the start sweep and the reconnect loop; test mode builds none of them.
- **Web**: the trade-mode confirm (202 with LIVE_SEND; 409 BATCH_STALE, NOT_CONNECTED, BATCH_EXPIRED), `POST /api/live/batches/{id}/cancel`, `GET /api/ibguard/preview`, `POST /api/ibguard/thresholds` (204; 409 PREVIEW_CHANGED or IBGUARD_UNREACHABLE; 400 NO_IBGUARD_EXIT), `services.ibguard` from what btlab last saw of ibguard, and `positions_seen` in batches. 51 API routes.
- **UI**: the Live view warns when sells are not coordinated with ibguard; the confirmation dialog names the account kind (live in warning colour) and the positions whose protection the orders lift; [Cancel working orders] with a confirmation; the ibguard panel (backtest, preview with account, paused state, current and new thresholds and the positions that would exit, a confirmed write). Strings and codes in both locales; the shell version follows.
- **Emails**: BATCH_DONE, PROTECTION_RESTORED, IBGUARD_RESTORE_FAILED, READ_ONLY_API.
- **Tests** (AC-26, AC-27; `tests/test_trading.py`, 25 tests, with `FakeOrderPort`, `FakeAccount` and `FakeIbguard` in `tests/fakes.py`, plus `test_cli.py` and `test_web.py`): limits (value, share, count keeping the largest), price-rule rounding, trade lines from IB's positions with every skip; ticket fields (MKT/OPG, LMT/DAY on the grid, outside RTH off, account, unique `btlab:` orderRef) with reducing lines first; the send function's four refusals and the limits again; BATCH_STALE, NOT_CONNECTED and BATCH_EXPIRED; line states, fills, a late commission and BATCH_DONE; READ_ONLY_API stopping the batch; recovery by the exact orderRef and other orders left alone; cancelling only btlab's working orders; the adapter on a stand-in IB (order fields, cancelling only its own `btlab:` orders, state mapping); the source scan (`placeOrder` and `cancelOrder` only in `ib_orders.py`, `reqGlobalCancel` nowhere); the preview (long and short, take-profit and stop-loss, unknown prices, STK only, token, ranges); the sell protocol against the fake ibguard (exclude, verify, send, restore with its email; CLOSED when flat; EXITING and pending exits skipped; the owner's exclusion kept; an exclusion ibguard does not show skipped; unreachable ibguard skipping sells but not buys; a failing include retried with one event and one email); the sweep expiring day orders; the trade-mode routes, the preview and write routes, IBGUARD_UNREACHABLE without the token in the message; and only trade mode constructing the order adapter at startup.
- **Smoke run** (scratch server, test mode): the Live view as in prompt 22, and the ibguard panel; no page errors.
- `.venv/bin/python -m pytest -q`: 1271 passed (also with `-X dev -W error`, after one run where the older optimization test above raced).
