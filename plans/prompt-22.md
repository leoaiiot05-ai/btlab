# Prompt 22: Live portfolios and test mode

Covers SPEC.md §14, §15.1, §15.2 (lots), §15.4 (limit prices), §17.1, §17.2 (follow-ups), §17.4 (cutoffs), §17.6 (BATCH_READY), §19 (live routes, the state document) and §20.2 item 7. Acceptance: AC-25 and the live part of AC-29.

## 1. Steps

1. **`btlab/live.py`**, pure:
   - `replay_targets`: the replay's positions after the last close, each pending order's target in its place;
   - `target_weights`, `live_lot` (US 1; TW the contract's size increment, else 1,000) and the target quantities rounded toward zero to the lot;
   - `batch_lines`: Δ = target − current, lines under one lot dropped, SKIPPED_TW_SHORT and SKIPPED_NO_PRICE kept with their reason; order kind (US per `orders.us_order_kind`, TW LIMIT) and limit price (reference ± the offset, TW clipped inside the session's price limits);
   - `cutoff`: 09:20 America/New_York (US), 08:50 Asia/Taipei (TW) on the session day;
   - the virtual book: corporate actions at the open (share ratio: trunc, remainder in cash at the previous raw close ÷ r; cash dividend net of withholding for longs, paid by shorts), the fill price of a line on its session bar (MOO at the open; LIMIT buy at the open when open ≤ limit, else at the limit when low ≤ limit, else expired; sells mirrored; an untradable bar, or a lock against the line's side, expires it), slippage (never past the limit), commission and TW transaction tax, then the ledger row (cash, equity at the close, holdings).
2. **Store** (`records.py`, `store.py`): portfolios (create, unique active per market and mode → ALREADY_EXISTS, list, deactivate), batches with their lines (save, replacing a PROPOSED batch as REPLACED in one transaction; read; status), line updates, the virtual holdings and the ledger.
3. **Jobs** (`jobs.py`):
   - the LIVE lane: `LIVE_GENERATE` (the replay of the stored configuration through the latest session in a pool worker; live equity from the virtual book; the lines; the batch for the next session; event BATCH_PROPOSED; the BATCH_READY email; the replay's and the benchmark's curves kept for the chart) and `LIVE_VIRTUAL_FILL` (each stored session since the last ledger row: corporate actions, the fills of that session's confirmed lines, the ledger row; a session without a bar expires its lines; a batch whose lines all ended is DONE);
   - follow-ups after an update ending UPDATED or PARTIAL: LIVE_VIRTUAL_FILL in test mode, then LIVE_GENERATE, which needs a bar or a no-trade day on the latest session for every symbol of the portfolio and otherwise records LIVE_DATA_INCOMPLETE and makes no batch;
   - the scheduler's tick expires PROPOSED batches at their cutoff (event BATCH_EXPIRED);
   - confirming in test mode: a PROPOSED batch before its cutoff; checked lines become VIRTUAL_PENDING, unchecked ones SKIPPED_OWNER; event BATCH_CONFIRMED. Trade mode answers NOT_CONNECTED until prompt 23.
4. **Routes**: `GET /api/live` (the run mode's portfolios with holdings, cash, equity and the latest batch), `POST /api/live` (201; 409 ALREADY_EXISTS; 400 NOT_DAILY), `DELETE /api/live/{id}`, `GET /api/live/{id}/curve` (portfolio, benchmark and replay rescaled to the first session), `GET /api/live/{id}/batches`, `POST /api/live/{id}/regenerate` (202; 409 BATCH_LOCKED or JOB_RUNNING), `POST /api/live/batches/{id}/confirm` (202; 409 BATCH_EXPIRED; 409 NOT_CONNECTED in trade mode). The state document's `live` part.
5. **UI**: the Live view for test mode, one card per market: the portfolio and its holdings, the curve, the current batch with checkboxes and the cutoff countdown, [Confirm] with a dialog listing the orders, [Regenerate], the batch history, and creating a portfolio from a daily backtest. Strings in both locales; the shell version follows.
6. **Tests** (AC-25, `tests/test_live.py`), with temporary stores, synthetic data and the injected clock: creation rules; replay targets with a pending order; live lots; each skip; replacement and expiry; virtual fills (MOO, limit buys and sells, an untradable and a locked bar, costs); a split and a dividend on the virtual book; ledger rows and the curves; the follow-ups and LIVE_DATA_INCOMPLETE; the routes; and test mode never constructing the order adapter.
7. Run the suite and smoke-test the Live view in headless Chromium.

### Readings where the spec is silent

- **Valuation prices.** The replay's weights use its own valuation close (raw in REALISTIC, adjusted in ADJUSTED), consistent with its equity; live quantities use the raw last close, carried over a no-trade day.
- **A batch's session** is the next trading day after the latest stored session (§4.3: an unknown later day counts when it is a weekday). If that day brings no bar (a holiday), its confirmed lines expire when a later session is stored.
- **Virtual shorts** (US) pay their dividends; borrow fees are not charged on the virtual book (§14.5 names corporate actions only).
- **Limit fills** take slippage too, but never past the limit price.
- **The replay and benchmark curves** are kept by LIVE_GENERATE (in `settings`), so the chart needs no replay of its own; both are rescaled to the capital at the session before the first.
- **SKIPPED_NO_PRICE** (a symbol without any close) is kept with quantity 0.

## 2. Extension points

- **Trade mode** (prompt 23): live equity from ledger cash and IB positions, SKIPPED_NO_CONTRACT, BLOCKED_LIMIT, sending on confirmation, line states after SENT, all through the same batch and line records.
- **The ibguard panel** joins the Live view in prompt 23.

## 3. Refactor seams

- `live.py` takes plain values (aligned bars, a cost model, quantities), so trade mode reuses the targets, lots and lines.
- `backtest.py` reports the replay's final positions and pending targets with the result.

## 4. Deferred items (appended to TODO.md)

- None.

## Manual checklist (on the PC, test mode)

1. Backtest view: run a daily US backtest of 3–5 symbols; Live view: create a US portfolio from it (default capital).
2. After the next US update (or [Regenerate]), the batch appears with its lines, the cutoff countdown and any skips with their reason; Home shows the batch.
3. [Confirm] with one line unchecked: the dialog lists the orders; the unchecked line shows SKIPPED_OWNER.
4. After the following update: the confirmed lines show FILLED or EXPIRED, the holdings and cash change, and the curve shows the portfolio against the benchmark and the replay.
5. Leave a batch unconfirmed past its cutoff (09:20 New York): it shows EXPIRED.
