# Prompt 2: Store

Covers SPEC.md §6.1, §6.6, §7 and §18, the lock and time lines of §23, and the values that §4.1, §5.1 to §5.4 and §5.6 store. Acceptance: AC-2.

## 1. Steps

1. **`btlab/records.py`** (new, pure: imports only `dataclasses`, `datetime` and `numpy`). It holds the records the store reads and writes, so the pure core (prices.py, calendar.py, intraday.py, backtest.py) can use them without importing the store.
   - Codes:
     - `CATEGORY_SOURCES`: RULE, OWNER (§7).
     - `ACTION_KINDS`: CASH_DIV, SHARE_RATIO (§6.2).
     - `COVERAGE_STATUSES`: OK, EMPTY, FAILED, NO_PERMISSION, MISMATCH (§5.4).
     - `EVENT_LEVELS`: INFO, WARNING, ERROR.
     - `EVENT_KINDS`: the 30 kinds of §18, in its order.
     - Prompt 17's locale test collects codes from constants like these.
   - Units: a day is an integer count of days since 1970-01-01, and a minute bar is stamped with the UTC epoch second of its start (§3). Times are aware UTC `datetime`s.
   - Records are frozen dataclasses. A record that holds arrays is `eq=False`, so `==` never compares arrays element by element. Tests compare those arrays with `numpy.testing`.
     - `SymbolInfo(market, symbol, name, category, category_source, exchange, minute_enabled, borrow_fee_pct, last_fetch_at, last_fetch_error, added_at)`.
     - `SymbolList(id, name, market, created_at, symbols)`, with the symbols in list order.
     - `DailyBars(day, open, high, low, close, volume, no_trade)`: one symbol's raw bars in day order. `day` is int64, prices and volume are float64, and `no_trade` is bool. A TW no-trade day has NaN prices (§6.1).
     - `Action(day, kind, detail, cash=0.0, ratio=1.0, ref_before=None, ref_after=None)` (§6.2).
     - `ShortSuspension(start_day, end_day, reason)` (TW, §5.2).
     - `RiskFreeRates(day, rate_pct)`: DTB3 in percent. Days without a value are absent (§5.3).
     - `Contract(con_id, exchange, currency, min_size, size_increment, market_rule_ids, fetched_at)` (§5.4, §15.2). `market_rule_ids` is IB's comma-separated text, kept as given.
     - `ExternalSeries(name, first_day, values, in_sample)` and `ExternalSnapshot(taken_at, series)` (§5.6).
     - `Event(id, ts, level, kind, market, symbol, message)`.
     - `JobRun(id, lane, kind, scheduled_for, started_at, finished_at, outcome, message)`.
     - `MinuteBars(t, open, high, low, close, volume)` and `MinuteCoverage(day, status, factor, fetched_at)` (§5.4, §6.6).
2. **`btlab/store.py`: files, pragmas, schema and errors** (§18, §23).
   - **`StoreError(message, conflict=False)`** is the one error type.
     - Every `sqlite3.Error` raised while opening, reading or writing becomes a `StoreError`. The message names the file and carries SQLite's text. That text never holds a bound value, and the store holds no secret (§21).
     - `conflict` is true when a UNIQUE or PRIMARY KEY constraint failed (from `sqlite_errorname`). Prompt 15 needs it to answer 409 ALREADY_EXISTS for a list name, and prompt 12 for a rule name.
     - An invalid argument raises `ValueError` before anything is written.
   - **The store reads no clock.** Callers pass every time from the app's injected clock (§23), so tests fix all times.
   - **Opening.** The private base class `_Database(folder)` opens `folder / FILE_NAME` and creates the folder if needed. In prompt 15's wiring the folder is `config.root / "data"` (§22). If any step fails, it closes the connection, so Windows can delete the file, and raises `StoreError`. The steps, in order:
     1. `sqlite3.connect(path, timeout=1.0, autocommit=True)`. This sets the busy timeout to 1000 ms, and every transaction is explicit.
     2. Read `PRAGMA user_version`. A value above 1 means a newer btlab wrote the file: raise `StoreError` before anything changes it.
     3. Run `PRAGMA journal_mode = WAL`, then `PRAGMA synchronous = NORMAL`. WAL may not be available (some external drives): the pragma answers something other than `wal`, or raises `sqlite3.OperationalError`. Then log a warning that names the file and the reason, and set `PRAGMA journal_mode = DELETE` with `synchronous = FULL`. If DELETE cannot be set either, raise `StoreError`.
     4. Run `PRAGMA foreign_keys = ON`.
     5. At `user_version` 0, in one transaction: create this file's part of the §18 schema, verbatim plus `IF NOT EXISTS`, then set `PRAGMA user_version = 1`. The script runs through `executescript` inside the explicit transaction, because with `autocommit=True` it neither opens nor commits a transaction itself. Splitting the script on `;` would break on a schema comment that contains one.
   - **Transactions.** `_write()` is a context manager: `BEGIN IMMEDIATE`, then `COMMIT`, or `ROLLBACK` on any exception. Every access function writes in one transaction, and a failed write changes nothing. `_read(sql, params)` maps errors the same way. `close()` closes the connection, and `path` names the file.
   - **Private conversions:**
     - times to and from UTC ISO 8601 text with microseconds and `Z`; a naive `datetime` raises `ValueError` (§23);
     - float64 arrays to and from little-endian BLOBs (`<f8`, 1-D only, §18);
     - NaN binds as NULL, and NULL reads back as NaN in float arrays;
     - settings values as JSON, with `allow_nan=False`.
   - **The process lock is not here.** Prompt 15's `__main__` takes `data/btlab.lock` before it constructs the stores (§23). The busy timeout is for other connections, such as a database browser.
3. **`Store(folder)` for `data/btlab.db`.** Some writes decide a data update's outcome: §17.2 says UP_TO_DATE when nothing new is stored. Those writes return the number of rows added, changed or removed.
   - **Symbols (§7):**
     - `add_symbol(market, symbol, category, added_at) -> bool` adds a row with category_source RULE. If the symbol already exists, it returns False and leaves the row as it was. A category the market does not have raises `ValueError` (§3, §4.1).
     - `symbols(market) -> list[SymbolInfo]`, sorted by symbol.
     - Setters. Each returns False for an unknown symbol:
       - `set_symbol_details(market, symbol, name, exchange) -> bool`, from TaiwanStockInfo (§5.2);
       - `set_category(market, symbol, category) -> bool`, the owner's override, which sets OWNER;
       - `set_minute_enabled(market, symbol, enabled) -> bool`;
       - `set_borrow_fee(market, symbol, pct | None) -> bool`, where None means the run's default;
       - `record_fetch(market, symbol, at, error | None) -> bool`, which sets `last_fetch_at` and `last_fetch_error`. The error is SourceError's code and text (§5.7), and None clears it.
     - `purge_symbol(market, symbol) -> bool` deletes the row with its bars, actions and contract, and its short suspensions in TW (§7). Removing a symbol from lists deletes nothing. The IN_USE check belongs to the web layer (§19).
   - **Lists (§7).** Functions that take an id return False for an unknown one.
     - `create_list(name, market, created_at) -> int`. A name already in use raises `StoreError` with `conflict`. An unknown market raises `ValueError`.
     - `rename_list(list_id, name) -> bool`.
     - `set_list_symbols(list_id, symbols) -> bool` replaces the members, in the given order. A repeated symbol raises `ValueError`. A symbol can be in several lists.
     - `delete_list(list_id) -> bool`. The members go with the list through ON DELETE CASCADE.
     - `lists(market=None) -> list[SymbolList]`.
   - **Daily bars (§5.1, §5.2, §6.1):**
     - `upsert_daily_bars(market, symbol, bars) -> int` inserts new days and replaces stored days of the window. Every other stored day stays, including a stored day that the new answer lacks. The write is `INSERT ... ON CONFLICT DO UPDATE ... WHERE` some column differs. The comparison uses `IS NOT`, so NULL prices compare too, and an unchanged overlap counts 0. Arrays of different lengths raise `ValueError`.
     - `daily_bars(market, symbol, first_day=None, last_day=None) -> DailyBars`, with inclusive bounds.
     - `daily_bar_range(market, symbol) -> (first, last) | None` serves the windows of §5.1 and §5.2, the data range of §19 and the data stamp of §10.11.
   - **Actions (§6.2):**
     - `replace_actions(market, symbol, first_day, last_day, actions) -> int` replaces the stored actions inside the window, so a corrected or withdrawn action does not linger. The windows are 400 days in §5.2 and 7 days in §5.1. An action outside the window, or an unknown kind, raises `ValueError`. Two actions of one kind on one day break the primary key and change nothing.
     - `actions(market, symbol, first_day=None, last_day=None) -> list[Action]`, sorted by day and kind.
   - **Trading days (§4.3):**
     - `replace_trading_days(market, days) -> int` replaces the market's whole set. TaiwanStockTradingDate takes no parameters, so each answer is the whole calendar.
     - `trading_days(market) -> np.ndarray`: int64, ascending.
     - US trading days are the days of stored SPY bars, read with `daily_bars("US", "SPY")`.
   - **Short suspensions (§5.2, §10.7):** `replace_short_suspensions(symbol, first_day, last_day, suspensions) -> int`, with the window on `start_day`, and `short_suspensions(symbol) -> list[ShortSuspension]`.
   - **Risk-free rates (§5.3):** `upsert_rf_rates(rates) -> int` and `rf_rates(first_day=None, last_day=None) -> RiskFreeRates`.
   - **Contracts (§5.4):** `save_contract(market, symbol, contract)` inserts or replaces the row; `contract(market, symbol) -> Contract | None`.
   - **External series (§5.6):**
     - `replace_external_series(snapshot)` deletes the stored set and inserts the snapshot's series in one transaction. `values_blob` is float64 little-endian, and `snapshot_at` is the snapshot's time.
     - `external_snapshot() -> ExternalSnapshot | None`.
   - **Settings (§4.1, §5.1, §5.2)**, under the keys `data_start/<market>` and `request_log/<source>`:
     - `data_start(market) -> int | None`.
     - `set_data_start(market, day) -> int` inserts only when no value is stored, and returns the stored day, so the first value stays. An unknown market raises `ValueError`.
     - `request_log(source) -> list[float]`, empty when nothing is stored, and `save_request_log(source, times)`. The log holds the UTC epoch seconds of past requests, as prompt 3's budget keeps them, so a restart reloads it.
   - **Events (§18, §19):**
     - `append_event(ts, level, kind, message, market=None, symbol=None) -> int`. The level must be in `EVENT_LEVELS` and the kind in `EVENT_KINDS`, otherwise `ValueError`, so every stored code can be localized (§20.4).
     - `latest_events(limit) -> list[Event]`, newest first by id, not by `ts`.
   - **Job runs (§17.1, §17.4):**
     - `start_job_run(lane, kind, started_at, scheduled_for=None) -> int`.
     - `finish_job_run(run_id, finished_at, outcome, message=None) -> bool`.
     - `latest_job_runs(kind, limit) -> list[JobRun]`, newest first. It serves the catch-up check and the state document's `last_update`.
     - Lanes, kinds and outcomes are prompt 14's codes, and the store does not check them.
   - **The other btlab.db tables** get access functions in the prompts that use them: results in prompt 8, rules in 12, optimizations in 13, live portfolios in 22 and ibguard exclusions in 23.
4. **`MinuteStore(folder)` for `data/minute.db`** (§5.4, §6.6).
   - `save_minute_day(market, symbol, coverage, bars=None)` writes the day's bars (insert or replace by `t`) and its coverage row in one transaction, so coverage OK never exists without its bars. Bars with a status other than OK or MISMATCH raise `ValueError`, and so does a status outside `COVERAGE_STATUSES`.
   - `minute_bars(market, symbol, first_t=None, last_t=None) -> MinuteBars`, with inclusive bounds.
   - `minute_coverage(market, symbol, first_day=None, last_day=None) -> list[MinuteCoverage]`, sorted by day.
   - `purge_symbol(market, symbol)`. A purge spans both files, so the caller (prompt 15) purges minute.db first. Then a failure leaves the symbol listed, and the purge can be repeated.
5. **Deviations from SPEC.md, and readings where it is silent:**
   - **A new module, `btlab/records.py`**, not in §2's starting layout. The records cross layers: prices.py (pure, prompt 4) produces `Action`s, the data clients and ib_link produce bars, and the engine reads suspensions. If store.py defined them, pure modules would have to import the persistence adapter.
   - **`PRAGMA foreign_keys = ON`**, not in §18's pragma list. SQLite enforces foreign keys only with this pragma, and the schema's ON DELETE CASCADE needs that.
   - **synchronous in the DELETE fallback.** §18 pairs NORMAL with WAL and says nothing for DELETE. The store uses FULL, SQLite's default. With NORMAL, a power loss can corrupt a rollback-journal database on older file systems, and the fallback exists for external drives.
   - **The schema** is §18's verbatim, plus `IF NOT EXISTS`, which SQLite drops from the stored schema text. No table, column or index differs.
   - **Window semantics**, which §5.1, §5.2 and §18 leave open:
     - Bars are upserted. A stored day missing from a new answer stays, so a short answer never deletes history.
     - Actions and short suspensions replace their window, so corrections and withdrawals take effect.
     - TW trading days replace the whole set.
   - **The purge** also deletes the `symbols` row and the TW short suspensions, which §7 does not list.
   - **Action details.** The store does not check `detail`. §6.2 names no detail for a US cash dividend (Tiingo's `divCash`), so prompt 4 chooses one. The column stays NOT NULL.
   - **One action per kind and day.** The primary key `(market, symbol, day, kind)` holds one action of each kind per day. When one day has several share events, which multiply (§6.3), prompt 4's normalization merges them before storing.
6. **No other file changes.**
   - New files: `btlab/records.py`, `btlab/store.py` and `tests/test_store.py`. TODO.md gets the items of section 4.
   - `__main__.py` is wired in prompt 15: the process lock first, then the stores (§23).
   - No dependency is added.
7. **Tests** in `tests/test_store.py`, against temporary folders. No test touches the network or IB.
   - Fixtures open the stores and close them afterwards, so Windows can delete the files.
   - All times are fixed aware UTC datetimes.
   - Raw `sqlite3` connections check what the API cannot show.
   - **Files and schema:**
     - `test_both_files_hold_the_schema_of_section_18`:
       - Opening both stores in a missing folder creates `btlab.db` and `minute.db`.
       - Each file's `sqlite_master`, with comments stripped and whitespace collapsed, equals that of its part of the §18 SQL block. The test reads the block from SPEC.md and runs it in memory.
       - `user_version` is 1.
       - btlab.db has no minute table, and minute.db has exactly `minute_bars` and `minute_coverage`.
     - `test_schema_creation_is_idempotent`, for both files. Reopening keeps the schema and the rows. With `user_version` reset to 0, the creation runs again over the existing tables without an error or any loss.
     - `test_a_newer_schema_version_is_refused` and `test_a_file_that_is_not_sqlite_is_a_store_error`.
     - `test_the_pragmas_of_section_18`, for both files: journal mode `wal`, synchronous NORMAL, busy timeout 1000 and foreign keys on. The per-connection pragmas are read through the store's private connection.
     - `test_wal_falls_back_to_delete_with_a_warning`, for both files:
       - The patch is a `sqlite3.Connection` subclass, passed as `factory`, whose WAL pragma either answers `delete` or raises `disk I/O error`.
       - After opening, the journal mode is `delete` and synchronous is FULL. The warning names the file, and writes and reads work.
       - If DELETE is refused too, opening raises `StoreError`.
     - `test_a_write_against_a_locked_database_fails_within_the_busy_timeout`, for both files. A raw connection holds `BEGIN IMMEDIATE`. A store write then raises `StoreError` after at least 0.9 s and in under 5 s. Once the lock is released, the same write succeeds.
     - `test_a_failed_write_changes_nothing`: bars whose last row has a NaN volume (NOT NULL) raise `StoreError`, and none of the call's rows is stored.
     - `test_times_are_utc_iso_8601`: a time at +08:00 is stored as UTC text ending in `Z` and reads back as the same instant. A naive time raises `ValueError`.
     - `test_tables_without_access_functions_round_trip`, parametrized over the 11 tables of results, rules, optimizations, live portfolios and exclusions. A full row inserted through a raw connection reads back equal, so AC-2's "every table round-trips" holds now. Prompts 8, 12, 13, 22 and 23 move their tables to round trips through their access functions.
   - **Access round trips**, one test per group of tables:
     - Symbols:
       - every setter;
       - `add_symbol` leaves an existing symbol as it was;
       - unknown symbols;
       - a category the market does not have.
     - Lists:
       - order, and a symbol in two lists;
       - rename, and replacing the members;
       - delete, with the members cascading;
       - a duplicate name raising `conflict`, and a repeated symbol.
     - Daily bars:
       - a TW no-trade day keeps its NaN prices, volume and flag;
       - range bounds, and `daily_bar_range`;
       - an empty read has the right dtypes;
       - arrays of different lengths are rejected.
     - `test_upserting_an_overlap_window_replaces_only_its_days`:
       - Days 1 to 10 are stored, then days 8 to 12 are upserted, with 9 and 10 revised.
       - Days 1 to 8 are bit-identical, and days 9 to 12 hold the new values.
       - The count is 4, and the same upsert again counts 0.
       - Another symbol is untouched.
     - Actions:
       - every field;
       - window replacement: actions inside the window are replaced or removed, actions outside it are untouched, the count is right, and an identical replacement counts 0;
       - an action outside the window, and an unknown kind, are rejected;
       - a duplicate key changes nothing.
     - Trading days, short suspensions, risk-free rates (counts and bounds) and contracts (replace, unknown).
     - `test_external_series_round_trip_as_float64_little_endian`: NaN, ±inf, −0.0, a subnormal and a big-endian input read back bit-identical as native float64. The raw BLOB equals `struct.pack("<…d", …)`.
     - `test_a_snapshot_replaces_the_set_in_one_transaction`: a second snapshot replaces the first. A snapshot with a repeated name raises `StoreError` and keeps the previous set.
     - `test_data_start_is_set_once`: None at first, then the first day. A second call does not change it. Each market has its own, and both survive a reopen.
     - `test_request_logs_survive_a_reopen`: both sources, exact floats and an overwrite. Non-finite times are rejected.
     - `test_events_come_back_newest_first`: five events with timestamps out of order. `latest_events(3)` gives the last three, newest first, with every field intact. An unknown level or kind is rejected.
     - `test_event_kinds_are_those_of_section_18`, against the list in SPEC.md.
     - Job runs:
       - start, with and without `scheduled_for`;
       - finish;
       - filtering by kind, newest first;
       - unfinished runs, and unknown ids.
     - Minute bars and coverage:
       - OK and MISMATCH days with bars and a factor;
       - EMPTY, FAILED and NO_PERMISSION days without bars;
       - a FAILED day saved again as OK;
       - bounds on `t`;
       - bars with EMPTY are rejected.
     - `test_purge_deletes_only_that_symbols_data`: in both files. The other symbol and the lists are untouched.
   - **AC-2, clause by clause:**

     | Clause | Test |
     |---|---|
     | Schema creation is idempotent | `test_schema_creation_is_idempotent` |
     | Every table round-trips | the access round trips and `test_tables_without_access_functions_round_trip` |
     | The overlap window replaces only its days | `test_upserting_an_overlap_window_replaces_only_its_days` |
     | Minute bars and coverage live in minute.db | `test_both_files_hold_the_schema_of_section_18` and the minute round trips |
     | The WAL fallback works | `test_wal_falls_back_to_delete_with_a_warning` |
     | A locked write fails within the busy timeout | `test_a_write_against_a_locked_database_fails_within_the_busy_timeout` |
8. Run `python -m pytest -q` from the venv until it passes: `.venv/Scripts/python` on the PC, `.venv/bin/python` in a cloud session.

## 2. Extension points

- **A table's access functions** are methods on the store class of its file, built on `_write`, `_read` and the time and BLOB conversions:
  - results in prompt 8, with `backtest_series` through the same BLOB conversions;
  - rules in prompt 12, using `StoreError.conflict` for names;
  - optimizations in prompt 13;
  - live tables in prompt 22;
  - exclusions in prompt 23.
- **A persisted value** is one pair of typed accessors over `settings`, with a key `<name>/<qualifier>` and a JSON value. Candidates, if prompts 3 and 14 keep them: the day of the last TaiwanStockInfo fetch (at most once a day, §17.2), or the month's Tiingo symbols (§5.1).
- **A record shared** by the pure core and an adapter goes in `records.py`. Example: prompt 3's typed FinMind action rows, which prices.py normalizes and must read without importing aiohttp.
- **An event kind** is one entry in `EVENT_KINDS` and in §18.
- **A schema change** bumps `user_version` and adds an upgrade step where `_Database` reads the version.

## 3. Refactor seams

- `_Database` owns the connection, pragmas, schema and transactions, and the store classes hold only SQL and conversions. Moving a store to a thread (§26) touches `_Database` only.
- Persistence and records are separate, so the pure core never imports `store.py`.
- Times, arrays and settings values each pass through one pair of private conversions.
- One private function makes the journal decision, choosing journal mode and synchronous together.
- The store fixtures can move to `tests/conftest.py` when prompt 8 or 14 needs a store in its tests.

## 4. Deferred items (appended to TODO.md)

- Retention for `events` and `job_runs` (Operations). Both tables keep every row, a few thousand a year. Seam: `append_event` and `start_job_run` in `store.py`.
- Schema upgrades (Operations). The store creates schema version 1 and refuses a newer file; a later schema needs an upgrade step. Seam: where `_Database` reads `user_version`.
