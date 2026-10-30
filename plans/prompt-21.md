# Prompt 21: Minute bars through IB

Covers SPEC.md §4.2, §5.4, §5.5, §6.6, §15.1 (the boundary), §17.1, §17.2 (the follow-up), §17.3 and §19 (`POST /api/minute/backfill`, the IB part of the state document). Acceptance: AC-24.

## 1. Steps

1. **`btlab/ib_link.py`**, the only module besides `ib_orders.py` (prompt 23) that imports `ib_async`; every name below was checked against the installed 2.1.0 source:
   - `HistoryPort`: the calls of ib_async's `IB` that history needs (`connectAsync`, `isConnected`, `disconnect`, `managedAccounts`, `reqContractDetailsAsync`, `reqHistoricalDataAsync`); production passes `IB()` with `RaiseRequestErrors = True`, tests a fake;
   - `IbLink(config.ib, mode, port, clock, sleep)`: connects on demand with the configured client id (17 is refused), `readonly=True` in test mode, the configured account and a 30 s timeout; the account kind (an account id starting with `D` is paper); `close()` at shutdown;
   - contracts: US `Stock(<symbol, "." as " ">, "SMART", "USD")`, TW `Stock(<code>, <recorded exchange>, "TWD")`, resolved with `reqContractDetailsAsync` (60 s timeout); only a unique match is used: conId, exchange, currency, minimum size, size increment and market-rule ids (unset sizes become null);
   - one request per symbol and session: `reqHistoricalDataAsync(contract, endDateTime=<session end, UTC>, durationStr="1 D", barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, formatDate=2, timeout=60)`; bars outside the regular session are dropped;
   - pacing: at least 2 s between requests on the injected clock; a pacing violation waits 60 s and retries once;
   - errors become `IbError` kinds: NO_DATA (IB's "no data", so the day is EMPTY), NO_PERMISSION (missing market-data permissions or subscription, or an unknown contract), PACING, FAILED (anything else, and an empty answer, which is ib_async's own 60 s timeout), UNREACHABLE (no connection, or the connection dropped).
2. **Normalization** (`prices.py`, pure): factor = stored daily close ÷ last minute close; within 2% of 1 → as returned; within 2% of the product of the share ratios of the actions after the day → prices × product, volumes ÷ product; otherwise as returned, MISMATCH.
3. **The MINUTE lane and MINUTE_BACKFILL** (`jobs.py`):
   - the queue: every symbol marked "minute bars" (live portfolios' symbols join in prompt 22), every day with a stored daily bar (no-trade days aside) from `backfill_years` before its latest bar, without coverage OK, EMPTY, NO_PERMISSION or MISMATCH; newest first;
   - per day: the contract (from the store, else resolved and cached), the bars, normalization, then `save_minute_day` with the coverage: OK, EMPTY, FAILED, MISMATCH (event MINUTE_MISMATCH) or NO_PERMISSION (event MINUTE_NO_PERMISSION, and no more requests for that market until its next update);
   - the per-market permission (OK or NO_PERMISSION) is a stored setting, for the state document and the stop;
   - progress: phase CONNECT then MINUTES, symbol and session, done and total, and an estimated end;
   - the pause while a DATA job runs, after which the queue is rebuilt (an update may have added sessions); cancel and shutdown between days; IB unreachable ends the job FAILED with `IB_UNREACHABLE: …`;
   - the follow-up: an update ending UPDATED or PARTIAL clears its market's permission stop and starts a backfill when IB is configured and the MINUTE lane is idle (a running backfill picks the new sessions up after its pause).
4. **Web**: `POST /api/minute/backfill {"market"?}` (202; 409 IB_NOT_CONFIGURED or JOB_RUNNING); `app.services.ib` in the state document (`configured`, `connected`, `account_kind`, `permissions`); `GET /api/symbols` already carries the minute flag and the coverage by status. `__main__.py` builds the link when `ib.host` is set and closes it at shutdown.
5. **UI**: the Data view gets the minute backfill (start and stop, its progress), the message when IB is not configured, and per market the NO_PERMISSION message; Home gets an IB card. New strings in both locales.
6. **Tests** (AC-24), with a fake `HistoryPort` and the injected clock (`tests/test_ib.py`):
   - the connection: client id, read-only in test mode (not in trade mode), the account, 30 s; client id 17 refused; the account kind;
   - contracts (US with a class share, TW with its exchange; unique, unknown, ambiguous);
   - the request's parameters, at least 2 s between requests, the pacing retry and the second violation, the error kinds;
   - normalization: a factor near 1, near the split product, and a mismatch;
   - the job: the queue newest first, coverage states, NO_PERMISSION stopping its market only, MISMATCH's event, IB unreachable, resumption after a cancel, the pause while DATA runs, the follow-up after an update;
   - a scan of the source tree: only `ib_link.py` and `ib_orders.py` import `ib_async`, and no test does;
   - the route, the state document.
7. Run `.venv/bin/python -m pytest -q` and smoke-test the Data view in headless Chromium.

### Readings where the spec is silent

- **A timeout is a failure, not an empty day.** ib_async's `reqHistoricalDataAsync` returns an empty list when its 60 s timeout passes, while IB's own "no data" answer is an error (162) once `RaiseRequestErrors` is on. So an empty answer is FAILED (retried later) and only IB's "no data" is EMPTY.
- **An ambiguous contract** (several matches) fails that symbol for the run without a permission stop: it is not a permission problem.
- **Manual starts respect the permission stop**; only the market's next update lifts it (§5.4).

## 2. Extension points

- **Live portfolios' symbols** (prompt 22) join the queue through one function.
- **Orders** (prompt 23): `ib_orders.py` shares the link's connection in trade mode.

## 3. Refactor seams

- `HistoryPort` is ib_async's own surface, so the fake mirrors IB and the request's parameters are tested as sent.
- Normalization is pure in `prices.py`, beside the daily corporate-action logic it relies on.

## 4. Deferred items (appended to TODO.md)

- None.

## Manual checklist (on the PC, IB Gateway on paper or with "Read-Only API")

1. `config.toml`: `[ib]` host `127.0.0.1`, port 4002 (paper), client id 31.
2. Data view: mark one US symbol "minute bars", start the minute backfill; the progress shows symbol, session and an estimated end; after a few sessions, stop it: its coverage shows OK days.
3. Start it again: it resumes with the sessions not yet covered (newest first).
4. Mark one TW symbol "minute bars" and start: either OK days, or NO_PERMISSION with the message in the Data view and an event MINUTE_NO_PERMISSION; US backfill is not affected.
5. Home shows IB connected and the account kind (paper).
6. Run a US update while a backfill runs: the backfill pauses, then resumes.

## Result

- **`btlab/ib_link.py`**: `HistoryPort` (ib_async's `IB` surface: `connectAsync`, `isConnected`, `disconnect`, `managedAccounts`, `reqContractDetailsAsync`, `reqHistoricalDataAsync`, all checked in the installed 2.1.0, including `RaiseRequestErrors`, `RequestError(reqId, code, message)`, `Stock(symbol, exchange, currency, conId=)`, `ContractDetails.minSize/sizeIncrement/marketRuleIds` and `formatDate=2` giving UTC datetimes). `IbLink` connects on demand (client id from the config, 17 refused; `readonly` in test mode; the configured account; 30 s), reports the account kind, resolves and describes contracts (unique match only; unset sizes null), and requests one session per call with the §5.4 parameters, 2 s pacing on the injected clock and one retry 60 s after a pacing violation. ib_async's `reqContractDetailsAsync` sends before it is awaited, so every request is made inside the error mapping. Error kinds: NO_DATA, NO_PERMISSION, PACING, FAILED (including ib_async's own 60 s timeout, which answers with nothing), UNREACHABLE.
- **`prices.py`**: `share_ratio_after` and `normalize_minutes` (within 2% of 1: as returned; within 2% of the later share ratios: prices × ratio, volumes ÷ ratio; else MISMATCH).
- **`jobs.py`**: the MINUTE lane's `MINUTE_BACKFILL` (phases CONNECT and MINUTES): the queue from the minute symbols' traded daily bars within `backfill_years` of their latest bar and the coverage table (FAILED retried), newest first; contracts cached in the store; coverage OK, EMPTY, FAILED, MISMATCH (event MINUTE_MISMATCH) or NO_PERMISSION (event MINUTE_NO_PERMISSION, and the market stops until its next update, through the setting `ib_permission/<market>`); progress with symbol, session and an estimated end; the pause while a DATA job runs, then a rebuilt queue; IB unreachable ends it `FAILED` with `IB_UNREACHABLE: …`. An update ending UPDATED or PARTIAL lifts its market's stop and starts a backfill when IB is configured and the lane is idle. `__main__` builds the link when `ib.host` is set and closes it at shutdown.
- **Web**: `POST /api/minute/backfill` (202; 400 for an unknown market; 409 IB_NOT_CONFIGURED or JOB_RUNNING); `app.services.ib` (`configured`, `connected`, `account_kind`, `permissions`). 41 API routes.
- **UI**: the Data view's "Minute bars (IB)" section (start and stop, the progress line, "IB is not configured" and the per-market NO_PERMISSION message); Home's IB card (connection, account kind, each market's minute-bar permission); 14 new strings in both locales; the shell version follows.
- **Tests** (AC-24, `tests/test_ib.py` with `FakeIB` in `tests/fakes.py`, plus `test_web.py`): the connection (read-only in test mode, not in trade mode, client id, account, 30 s, connecting once), client id 17 refused, an unreachable gateway, the account kind; contracts (a class share, TW with its exchange, unknown, error 200, ambiguous, TW without exchange); the request's parameters and the session filter; 2 s spacing and the pacing retry (sleeps 2, 60, 2, 60, then PACING); six answers and their kinds; normalization near 1, near the split product and a mismatch, and the share ratios after a day; the job (newest first, every coverage state, the split day ×2 with volumes ÷2, NO_PERMISSION stopping TW only, MISMATCH's event, contracts cached, a second run retrying only the FAILED day); resumption after a cancel; IB unreachable at connect and mid-run; the pause while a DATA job runs; the follow-up after an update; the scan (only `ib_link.py`, later `ib_orders.py`, imports ib_async; no test does); the route and the state document.
- **Smoke run** in headless Chromium over a scratch server (not `python -m btlab`), desktop and phone: Home's IB card, the Data view's backfill buttons (Stop disabled while idle) and the TW no-permission message; no page errors.
- `.venv/bin/python -m pytest -q`: 1214 passed (also with `-X dev -W error`).
