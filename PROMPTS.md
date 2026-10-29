# Build prompts for Claude Code

## How to use

Unattended (the normal way):

1. Run `bootstrap.ps1` once from the kit folder (see KIT-README.md). It:
   - creates `E:\00_CODING\btlab`;
   - initializes git with the GitHub remote, commits these files, `buildctl/`, `.claude/settings.json` and the vendored ECharts, and pushes;
   - writes `build.cmd`, then starts `build.cmd --cloud-after 1`.
2. `run_prompts.py` runs the prompts below in order, one headless Claude Code session each, with max effort and ultracode.
   - Each plan goes to `plans/prompt-NN.md` instead of waiting for approval.
   - After each prompt it runs the tests, asks the same session to repair a failure, then commits and pushes.
3. **Restarts.** After a restart (Windows Update included), the logon task starts the runner again. It continues the unfinished prompt, or, while the cloud holds the build, watches it in standby.
4. **Usage limits.** At a usage limit, it waits for the reset and continues.
5. **Cloud.**
   - `--cloud-after 1` runs prompt 1 on the PC, which must generate the Windows lock file, then hands the build over: it pushes, sets the baton in `buildctl/` to `cloud`, and starts `claude --cloud "Read CLOUD.md and continue the build."`. The PC then stays in standby and shows the cloud's progress.
   - A cloud scheduled task, every 2 hours, starts a new cloud session whenever the cloud holds the baton and no session is working (KIT-README.md). The build therefore survives the end of any one session.
   - `build.cmd --cloud` hands the build over at any time.
6. **Back to the PC.** `build.cmd --release` takes the baton back and waits until a working cloud session stops at its next checkpoint; add `--force` when that session is gone, for example because its usage ran out. The standby window also takes the build back by itself after the cloud has been idle for 6 hours.
7. `build.cmd --status` shows the progress, who holds the baton, the cloud lease and the last commit.

Interactive alternative: start Claude Code in the folder and paste the prompts in order, one per session. Each asks for a plan first: approve or correct it, let it implement, run the tests, commit, then start a new session for the next prompt.

Whenever Claude Code works on this PC, keep IB Gateway closed, logged in to the paper account, or set to "Read-Only API". The runner stops if a live API port is open.

If a result disagrees with `SPEC.md`, the spec wins. If the spec is wrong, change the spec first, then the code.

| # | Prompt | Acceptance criteria covered |
|---|---|---|
| 1 | Scaffold, lock file, config and tax table | AC-1 |
| 2 | Store | AC-2 |
| 3 | Data sources | AC-3 |
| 4 | Price series and calendars | AC-4, AC-5 |
| 5 | Indicators and features | AC-6 |
| 6 | Engine core | AC-7, AC-8, AC-9 |
| 7 | Exits and intrabar paths | AC-10 |
| 8 | Metrics, benchmark and the backtest runner | AC-11, AC-29 (part) |
| 9 | Built-in strategies | AC-12 |
| 10 | Plugins and the look-ahead check | AC-13 |
| 11 | Intraday backtests | AC-15 |
| 12 | Rule builder | AC-14 |
| 13 | Optimization and bench | AC-16, AC-17, AC-29 (part) |
| 14 | Jobs and scheduling | AC-18, AC-28 (notifier) |
| 15 | Web API core and wiring | AC-19, AC-20 (part), AC-28 (lock, order) |
| 16 | Web API for results and optimization | AC-20, AC-22 (API), AC-30 |
| 17 | Web UI: shell, data, backtest form and rule builder | AC-21 (part), UI checklist |
| 18 | Web UI: results, charts, comparison and optimization | AC-21, UI checklist |
| 19 | PWA and offline viewing | AC-22, offline checklist |
| 20 | spylead import | AC-23 |
| 21 | Minute bars through IB | AC-24 |
| 22 | Live portfolios and test mode | AC-25, AC-29 (part) |
| 23 | IB orders and ibguard coordination | AC-26, AC-27, paper checklist |
| 24 | Operations and README | AC-28, checks of SPEC.md §24.3 |
| 25 | Final audit | all |

---

## Prompt 1: Scaffold, lock file, config and tax table

```text
Read SPEC.md and TODO.md in full. CLAUDE.md is already loaded.

Goal: scaffold the repository. No data, engine or web code yet.

Build (SPEC.md §2, §4.1, the tax table of §10.6, §21, §22, and the logging line of §23):
- requirements.in with exactly numpy, aiohttp, tzdata, ib_async==2.1.0 and pytest. Then create the venv, upgrade pip, generate pylock.toml and install from it, as §21 describes. No pyproject.toml: the project runs from the repository root.
- Only the modules this phase needs: btlab/__init__.py, __main__.py, config.py, markets.py and taxes.py. Do not create placeholder files for later phases.
- `python -m btlab --config <path>`: parse the arguments, load and validate the config and taxes.toml, set up logging, then exit with a message that the app is not implemented yet.
- The config loader of §22, with errors that name the offending key and never echo a value, including the trade-mode limits rule and the client-id rule.
- markets.py: the constants of §4.1 as frozen dataclasses. Pure.
- taxes.toml exactly as in §10.6, and its loader, validation and lookup (§10.6). Pure after loading.
- config.example.toml as in §22, with mode "test" and [limits] commented out.
- .gitignore: config.toml, data/, logs/, .venv/, __pycache__/, .pytest_cache/, .claude-runs/, .claude/settings.local.json. Never ignore buildctl/ or .claude/settings.json.
- The vendored ECharts files are already in btlab/static/vendor/echarts/. Leave them as they are.

Tests:
- Config loading: a valid file, a missing file, an unknown key, a missing key, a wrong type, the placeholder token, a short token, client_id 17, trade mode with missing or non-positive limits, and empty API keys (accepted with a warning).
- No error message contains a value from the file (AC-1).
- The tax table: the committed file loads; overlapping ranges, a gap and an unknown category are rejected with messages naming the entries; the lookups of AC-1.
- A test runs `python -m btlab --config config.example.toml` in a subprocess and checks that it fails validation on the placeholder token, without touching the network.

Done when: pylock.toml holds a hash for every package, AC-1 holds, and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 2: Store

```text
Read SPEC.md §6.1, §6.6, §7, §18, and the lock and time lines of §23. Skim the existing code and follow its patterns.

Goal: persistence.

Build btlab/store.py:
- Open or create data/btlab.db and data/minute.db with the schema and pragmas of §18, including the WAL fallback and the busy timeout. One store class per file.
- Access functions for what the next phases need, and no more for now:
  - symbols, lists and list members;
  - daily bars (upsert of a window), actions, trading days, short suspensions, risk-free rates, contracts;
  - external series (replace the whole set in one transaction);
  - settings: data_start per market (set once), and the persisted request logs of §5.1 and §5.2;
  - events (append; the latest N, newest first) and job_runs;
  - minute bars and minute coverage.
- The schema creates every table now. Results, rules, optimizations and live tables get their access functions in the phases that use them.
- Arrays round-trip as float64 little-endian BLOBs.
- Write failures surface as one store error type.

Tests against temporary database files:
- schema creation is idempotent for both files;
- round trips for each table that has access functions;
- the overlap upsert replaces only the window's days;
- data_start is set once;
- latest-N ordering of events;
- the WAL fallback, with a patched pragma;
- a write against a locked database fails within the busy timeout with the store error type.

Done when: AC-2 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 3: Data sources

```text
Read SPEC.md §4.1, §5.1, §5.2, §5.3, §5.7, §6.2, §7, and the secrets rule of §21. Skim store.py.

Goal: the Tiingo, FinMind and FRED clients.

Build:
- btlab/http.py, the request rules of §5.7:
  - one session, passed in; timeouts; retries with an injected sleep; Retry-After;
  - the SourceError type with its codes, and the redaction helper;
  - a rolling request budget on an injected clock, whose log the caller persists.
- btlab/tiingo.py, btlab/finmind.py and btlab/fred.py:
  - the requests of §5.1, §5.2 and §5.3 with their headers, parameters and windows;
  - the budgets: Tiingo 45 an hour and 900 a day, FinMind 550 an hour;
  - MAINTENANCE for FinMind's Sunday window, SYMBOL_UNKNOWN, and MISSING_KEY without a request.
- Pure translation helpers, kept apart from the network calls:
  - Tiingo rows to bars and actions;
  - FinMind rows to bars (zero-close rows are no-trade days), trading days, stock info (name, exchange, each stock's latest row), short suspensions, and typed records of the dividend-result, dividend-policy, split, par-value and capital-reduction rows. Normalizing those TW records into actions belongs to prices.py (next prompt);
  - FRED observations to rates.
- The ticker mapping of §5.1 and the category rules of §7. Pure.

Tests, with aiohttp's test server or an injected fake session, and hand-written JSON samples:
- each request's path, parameters and headers, and that no key appears in a URL except FRED's api_key;
- the windows: the first fetch from data_start, later ones from 7 or 400 days back;
- retries, Retry-After, and no retry on 404, with an injected sleep;
- the budgets, including a restart that reloads the persisted log;
- MISSING_KEY without any request; MAINTENANCE; BAD_PAYLOAD for a missing field;
- every translation helper;
- every error text and every log line free of keys and tokens (AC-3).

Done when: AC-3 holds, pytest passes, and no test opens a real connection.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 4: Price series and calendars

```text
Read SPEC.md §3, §4, §6 and §7. Skim tiingo.py, finmind.py and store.py.

Goal: the pure price layer.

Build:
- btlab/prices.py:
  - normalization of US and TW actions into CASH_DIV and SHARE_RATIO with their details, by the rules of §6.2. BAD_ACTION and DIVIDEND_SPLIT_UNKNOWN are returned to the caller as values, not logged;
  - the adjustment factors and the adjusted OHLCV of §6.3;
  - alignment to a calendar (§6.4): bars × symbols arrays of raw and adjusted OHLCV, volume, tradable flags, and the per-day factors and actions the engine needs.
- btlab/calendar.py:
  - trading days of both markets (§4.3);
  - session start and end in UTC through zoneinfo (§4.2), and the next session after a day;
  - period starts, months and ISO weeks (§9.3);
  - the TW limit-lock rule of §6.5, with its direction.

Inputs are plain arrays and records from the store; outputs are arrays and dataclasses. No I/O, no clock, no logging.

Tests:
- AC-4: examples A1 to A6 of §6.3; each TW dividend case of §6.2; split, par-value and capital-reduction ratios; bad actions; adjusted volumes.
- AC-5: both calendars; sessions across US daylight-saving changes; alignment over missing days, no-trade days and a symbol listed mid-period; the limit lock before and after 2015-06-01, up and down, with the 0.9 margin, blocking only its direction.

Done when: AC-4 and AC-5 hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 5: Indicators and features

```text
Read SPEC.md §8. Skim prices.py and calendar.py.

Goal: the pure indicator layer.

Build:
- btlab/indicators.py: every indicator of §8.1 on bars × symbols arrays, causal and NaN-aware.
- btlab/features.py: the minute-derived daily features of §8.2, scaled by the day's adjustment factor; the today-so-far features of §8.3; and the alignment of daily values to minute timelines of §8.4.
- The registry of §8.6: names, arguments with types, defaults and ranges, outputs, and valid timelines. External series (§8.5) are registered by name at run time; the registry only needs that hook.

Tests (AC-6):
- every indicator against hand-computed values on small inputs, including the Wilder, EMA and MACD seeds, DONCHIAN without the current bar, and population against sample deviations;
- warm-up NaN and NaN propagation;
- truncation invariance for every indicator and feature: altering or removing bars after bar c changes nothing at bars up to c;
- the features of §8.2 and §8.3 on hand-made minute bars, including a session without coverage;
- §8.4: on a minute timeline, a daily value comes from the previous session.

Keep test data small.

Done when: AC-6 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 6: Engine core

```text
Read SPEC.md §3, §4.1, §6.5, §9.1, §9.4, §10.1 to §10.7, §10.10, §10.11 and §11.2. Skim prices.py, calendar.py, taxes.py and markets.py.

Goal: the pure simulation core. Exit overlays come in the next prompt.

Build:
- btlab/costs.py: the commissions, slippage, taxes (through taxes.py), day-trade rates, borrow fees, short-sale fee and withholding of §10.6.
- btlab/sizing.py: the sizing modes of §9.4.
- btlab/engine.py:
  - a timeline of bars with a session index, so one loop serves daily and minute timelines (§11.2 builds on it);
  - the order of events of §10.3, with a clear hook for step 3, intrabar exits;
  - DIRECTION, WEIGHTS with the rebalance mask, and ORDERS targets (§9.1); the held-quantity rule; lots; orders and fills; pending orders on untradable bars; reducing orders before increasing ones; pro-rata scaling (§10.4, §10.5);
  - shorting, buying power, borrow fees and TW forced covers (§10.7);
  - corporate actions in REALISTIC mode, and none in ADJUSTED mode (§10.2, §10.7);
  - trades with P&L net of attributable costs, average-cost partial reductions, and OPEN at the end (§10.3, §10.5);
  - the result of §10.11 without metrics (prompt 8), with the flags this phase can raise: SHORTS_IGNORED, DATA_ENDED.

Inputs are aligned arrays and plain dataclasses; outputs are arrays and dataclasses. No I/O, no clock, no logging.

Tests on hand-made bars of 2 or 3 symbols over a few dozen bars:
- AC-7: the accounting identity on every bar; both fill timings; slippage; lots and sub-lot orders; scaling; US and TW commissions; TW taxes by category and date, including both day-trade directions; trade P&L; partial reductions.
- AC-8: allow_short off and on; buying power with m = 0.5 and 0.9; the borrow fee by calendar days; US short dividends; the TW short-sale fee; forced cover and the no-short window.
- AC-9: share events with cash in lieu; stock dividends; cash dividends with withholding; pending targets scaled; value continuity on an ex-date; ADJUSTED mode.
- Determinism: two runs give identical arrays.

Done when: AC-7, AC-8 and AC-9 hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 7: Exits and intrabar paths

```text
Read SPEC.md §5.4 (minute coverage), §10.7 (levels under share events) and §10.8. Skim engine.py.

Goal: exit overlays and intrabar paths.

Build btlab/exits.py and wire it into step 3 of the engine:
- the overlays of §10.8 with their levels for longs and shorts, including the ibguard rule as a pure function of (side, ref, prev, point, thresholds);
- path builders: the daily path, the minute path from a day's 1-minute bars, and the path of a minute-timeline bar from its 1-minute bars;
- triggering along the path: a fill at the level inside a continuous segment and at the gap price after a gap; the earliest crossing wins; stops win ties;
- max-hold timing under both fill timings;
- level rescaling on share events (§10.7);
- re-entry only after the strategy's target changes (§10.8);
- PATH_FALLBACK counting when a day lacks minute coverage.

Tests (AC-10):
- each overlay on hand-made bars, long and short;
- the daily path for up and down bars, and a gap at the open;
- a minute path with a gap between two minutes;
- several overlays in one segment, and a tie;
- the ibguard vectors L1 to L10 and S1 to S5 of §10.8, fed as path points, give the listed ref values and exits;
- re-entry and PATH_FALLBACK.

Done when: AC-10 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 8: Metrics, benchmark and the backtest runner

```text
Read SPEC.md §5.3, §10.1, §10.9, §10.11, §12 and §17.5. Skim engine.py, exits.py, prices.py and store.py.

Goal: metrics, the benchmark, CSV exports, and one complete run from stored data.

Build:
- btlab/metrics.py: every metric and table of §12, the per-period risk-free rate from DTB3, and the benchmark's metrics. Pure.
- btlab/backtest.py:
  - run-configuration validation (§10.1), with INVALID_FIELD and the JSON path;
  - loading a run's data from the store into aligned arrays, with the warm-up;
  - running the engine, then the benchmark as BUY_HOLD through the engine (§10.9);
  - metrics, the flags SURVIVORSHIP (always) and BENCHMARK_PARTIAL, and the data stamp.
  - Built-in strategies arrive in the next prompt: for now the runner takes a targets function, and tests pass simple ones.
- CSV builders for trades.csv and equity.csv (§12).
- Store access for saving a result (backtest row, series, trades) in one transaction, and for reading it back.

Tests:
- AC-11 on a small hand-made equity curve and trade list, including the rf alignment, an unrecovered drawdown, α and β, null for undefined values, and the CSV columns with a BOM;
- configuration errors with their paths;
- a full run from a temporary store with synthetic data, saved and read back;
- the benchmark run and BENCHMARK_PARTIAL;
- two identical runs give identical results (part of AC-29).

Done when: AC-11 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 9: Built-in strategies

```text
Read SPEC.md §9.1 to §9.4. Skim indicators.py, sizing.py, calendar.py, engine.py and backtest.py.

Goal: the ten built-in strategies.

Build btlab/builtins.py:
- strategies A to J of §9.3 with their parameter schemas (§9.2) and outputs: DIRECTION; WEIGHTS with rebalance masks; ORDERS for DCA, through the engine's order interface;
- period starts from calendar.py, the rotation weightings, and DUAL_MOMENTUM's risk-free comparison;
- registration, so that backtest.py runs `strategy.source = "builtin"`.

Parameter validation (§9.2) is shared with plugins and rules: put it where all three can use it.

Tests (AC-12):
- each strategy on synthetic series built to trigger its rules: entries, exits, reversals, period starts, abs_filter, top_k with fewer candidates, vol-inverse weights summing to selected/top_k, safe asset and cash in DUAL_MOMENTUM, DCA running out of cash;
- every sizing mode through the engine, the held quantity, and the max_position_pct cap;
- truncation invariance of every built-in's targets;
- parameter validation errors.

Done when: AC-12 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 10: Plugins and the look-ahead check

```text
Read SPEC.md §9.1, §9.2, §9.5 and §9.7. Skim builtins.py, engine.py and backtest.py.

Goal: owner plugins in both styles.

Build:
- btlab/plugins.py:
  - discovery and loading from a strategies folder, the module contract of §9.5, PARAMS validation, and a load error per broken file;
  - the read-only Bars and Context objects;
  - the look-ahead check of §9.7 and the LOOKAHEAD_SUSPECT flag, with a function the optimizer can call for its first and best combinations (prompt 13).
- Plugins run through backtest.py (`strategy.source = "plugin"`). Context orders go through the engine's order interface.
- strategies/example_vector.py and strategies/example_event.py (§9.5): small, with comments only where a reason helps.

Tests (AC-13):
- both styles load from a temporary folder; the two examples load and run;
- each kind of broken plugin is listed with its error while the others load;
- Context exposes only rows up to t, and writing to a view raises;
- a plugin that reads one bar ahead is flagged; an honest one is not;
- plugins receive nothing but bars, params and ctx.

Done when: AC-13 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 11: Intraday backtests

```text
Read SPEC.md §8.3, §8.4, §10.8 (minute timelines) and §11. Skim engine.py, exits.py, features.py, backtest.py and store.py (minute bars).

Goal: backtests on minute timelines.

Build btlab/intraday.py and extend backtest.py:
- resampling of 1-minute bars to 5, 15, 30 and 60 minutes (§11.1);
- the minute timeline across the sessions with coverage, and untradable symbols without coverage (§11.2);
- loading minute bars from minute.db with their warm-up, plus the daily bars that §8.4 needs;
- fills within and across sessions (§11.3), close-out (§11.4), the day-trade tax (§11.5), the PDT flag (§11.6), NO_MINUTE_DATA, and session-end equity for the metrics (§11.7).

Tests (AC-15), on hand-made minute bars of 2 symbols over a few sessions:
- resampling with a short last bucket and a gap;
- fills at the next bar and across sessions;
- close-out with m = 0 and m = 30;
- a TW same-session round trip pays the day-trade tax;
- the PDT flag before and after 2026-06-04, and around USD 25,000;
- NO_MINUTE_DATA;
- metrics on session ends.

Done when: AC-15 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 12: Rule builder

```text
Read SPEC.md §8 and §9.6. Skim indicators.py, features.py, builtins.py, intraday.py and backtest.py.

Goal: the rule builder's backend.

Build btlab/rules.py:
- the JSON schema of §9.6 and its validation, with every error code and its JSON path;
- operand resolution through the registry of §8.6, with parameter substitution and the timeframe rules of §8.4 and §9.6;
- vectorized evaluation of conditions, `within`, `for` and the logical nodes, and the DIRECTION state machine;
- registration, so that backtest.py runs `strategy.source = "rule"`;
- store access for saving, listing and deleting rules by name.

Tests (AC-14):
- each validation code with its path; 3 levels pass and 4 fail; 33 conditions fail;
- each operator, cross_above and cross_below at their boundaries, within and for, NaN false;
- a rule equivalent to built-in A gives identical directions;
- a minute rule with "tf": "1d" uses the previous session; a daily rule with features; a minute operand on the daily timeline fails with TF_MISMATCH;
- truncation invariance.

Done when: AC-14 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 13: Optimization and bench

```text
Read SPEC.md §9.7, §13 and §24.3. Skim backtest.py, builtins.py, plugins.py and rules.py.

Goal: grid search, the out-of-sample check, walk-forward analysis, and the bench.

Build:
- btlab/optimize.py:
  - ranges by JSON path and the 2,000 limit (§13.1);
  - the grid with the injected pool, data shared through the pool initializer, and top-level worker functions (§13.2, §13.7);
  - the best combination with min_trades and ties; the heatmap (§13.3);
  - window runs, the out-of-sample check (§13.4), and walk-forward with the chained curve (§13.5);
  - the estimate, NEEDS_CONFIRMATION as a value the job layer turns into 409, progress callbacks and cancellation (§13.6);
  - the look-ahead check for a plugin's first and best combinations, and the propagation of IN_SAMPLE_SERIES and LOOKAHEAD_SUSPECT.
- Store access for saving and reading optimizations.
- btlab/bench.py: the three cases of §24.3 on synthetic data with the real pool, printing each case's time. Do not run it.

Tests (AC-16, AC-17, and the optimization part of AC-29):
- enumeration order, the limit, min_trades, ties;
- the heatmap;
- the 70% split and the warm-up of window runs;
- walk-forward windows, NO_RESULT, the dropped short window, the chained curve;
- the estimate and the confirmation threshold;
- a cancel keeps partial results;
- identical results with the in-process stand-in and with a real pool of 2 workers on a tiny case;
- bench.py imports and builds its data at a tiny size.

Done when: AC-16 and AC-17 hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 14: Jobs and scheduling

```text
Read SPEC.md §5 (budgets and windows), §17 and §23. §17.3 belongs to prompt 21 and the live follow-ups to prompt 22. Skim the data clients, prices.py, store.py, backtest.py and optimize.py. The jobs module composes them and must not duplicate their logic.

Goal: data updates, the scheduler, and backtest and optimization jobs, driven through fakes.

Build:
- btlab/jobs.py:
  - the lane runner: one job per lane, progress, cancellation and interruption (§17.1). The DATA and COMPUTE lanes now; MINUTE and LIVE come in prompts 21 and 22;
  - UPDATE_US and UPDATE_TW with their phases, outcomes and per-symbol failures (§17.2), normalizing actions through prices.py before storing them, and persisting the request budgets;
  - BACKTEST and OPTIMIZE jobs (§17.5) with the pool injected;
  - job_runs rows and events.
- btlab/scheduler.py: the due times of both schedules in Asia/Taipei, catch-up, retries and the MAINTENANCE retry (§17.4). Pure.
- btlab/notifier.py: the optional SMTP notifier of §17.6 behind a protocol. UPDATE_FAILED is wired now; the other emails arrive with their features.
- Keep-awake while a job runs (§23).

Tests, with fake sources, temporary databases, an injected clock and sleep, and the in-process pool:
- AC-18: one job per lane and 409; progress; cancel and interruption; UP_TO_DATE without inserts; UPDATED with overlap upserts; PARTIAL; FAILED on MISSING_KEY; MAINTENANCE; the wait for Tiingo's budget, with its progress message;
- the scheduler's due times on the right weekdays, one catch-up after downtime, and retries;
- a backtest job and an optimization job, saved through the store;
- the notifier raises nothing on SMTP failure and is off with an empty host (part of AC-28).

Done when: AC-18 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 15: Web API core and wiring

```text
Read SPEC.md §19, §21, §22 and §23. Skim jobs.py, store.py, plugins.py and rules.py.

Goal: the core of the JSON API and a runnable app.

Build:
- btlab/web.py, an aiohttp application with:
  - the session-cookie scheme, the body and number rules, and the error format of §19;
  - the §21 headers on every response, including errors and static files;
  - listening on 127.0.0.1 only;
  - static files from btlab/static/, with a minimal placeholder index.html until prompt 17;
  - these routes: login, logout, state (the markets, jobs and events parts; other parts arrive with their features), lists, symbols, update, job cancel, strategies and reload, indicators, and rules with validation.
  - The web layer validates input and calls the jobs module and the store. It holds no backtest logic.
- btlab/__main__.py: take the process lock first (§23), then wire config, taxes, stores, plugins, jobs with the real pool, the scheduler and the web server. Shut down in the order of §23.

Tests, with aiohttp's test utilities and a stub jobs object:
- AC-19 in full;
- login success and failure, with the 1 s delay injected, and logout;
- every route's success and error paths, and 409 while a lane is busy;
- the state document's shape;
- AC-28's runtime part: a second process exits while the lock is held, a lock left by a killed process does not block a restart, and the startup and shutdown order.

Do not start the app yourself.

Done when: AC-19 holds, the routes of this prompt meet AC-20, and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 16: Web API for results and optimization

```text
Read SPEC.md §10.11, §12, §13, §19 and §20.5. Skim web.py, backtest.py, optimize.py and store.py.

Goal: the routes for results, comparisons, exports and optimizations.

Build in web.py, with payloads as §19 describes:
- backtests: run, list, detail (updating opened_at), series, trades, candles with trade markers, the CSV exports, rename and keep_offline, and delete with IN_USE;
- GET /api/offline (§20.5) and compare;
- candles of stored symbols: daily and minute, adjusted or raw, in windows of at most 5,000 bars;
- optimizations: run (with TOO_MANY_COMBOS and NEEDS_CONFIRMATION), list, detail, heatmap, promote and delete.

Tests (AC-20, the API part of AC-22, AC-30):
- every route's success and error paths;
- the shapes of series and candle payloads: time units, rounding, null for NaN, windows;
- the CSV routes;
- compare with 2, 4 and 5 ids;
- the GET /api/offline selection (the 20 most recently opened plus keep-offline results) and opened_at updates;
- every flag of §10.11 arises in a scenario, is stored, and is served with its result.

Done when: AC-20 and AC-30 hold and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 17: Web UI: shell, data, backtest form and rule builder

```text
Read SPEC.md §19 (state document, payloads, error format), §20 and §21. Skim web.py.

Goal: the first half of the UI, for Edge on the PC and Safari on iPhone and iPad, installable as a PWA.

Build in btlab/static/, as §20 describes:
- index.html, app.js (split into a few ES modules if that helps; no build step) and style.css;
- manifest.webmanifest and icons/ (192, 512 maskable, and the 180 px apple-touch icon), generated by scripts/make_icons.py with the standard library;
- locales/zh-TW.json and locales/en.json;
- the shell: login, the tab bar or side bar, the run-mode badge, polling, the language toggle and logout;
- the Home, Data and Settings views (§20.2 items 1, 2 and 8);
- the Backtest view, with forms generated from the schemas, cost defaults per market, and field errors matched by JSON path (item 3);
- the rule builder (item 4).

Use the vendored ECharts only. No framework, no build step, no requests to other hosts. Server data enter the page through textContent and DOM APIs only.

Design brief:
- a research and trading console, not a marketing page;
- calm, dense, and legible on a phone held at arm's length;
- the run mode and the account kind impossible to miss, and a live account in red;
- status readable without colour; numbers aligned and formatted;
- forms usable with a thumb: steppers for numbers, large targets.

Tests (part of AC-21):
- the two locale files define the same keys and cover every code the backend can emit so far. Collect the codes from the Python enums and constants, so that later prompts extend the code, not a hand-made list;
- the static files outside vendor/ contain no innerHTML, insertAdjacentHTML, document.write, script element without src, style attribute or inline event handler;
- the manifest is valid JSON with start_url, scope and display, and its icon files exist as PNG of the stated sizes;
- the ECharts file matches its SHA-256.

Then write a manual checklist into the plan, covering:
- devices: Edge on the PC (installed as an app, light and dark, both languages), an iPhone at 375 px in portrait, and an iPad in both orientations through Tailscale Serve, added to the home screen;
- login and logout;
- lists and symbols, and an update with its progress;
- the backtest form with a built-in, a plugin and a rule;
- rule-builder validation errors.

Done when: the AC-21 checks pass, pytest passes, and the checklist is in the plan.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 18: Web UI: results, charts, comparison and optimization

```text
Read SPEC.md §12, §13, §19 and §20. Skim btlab/static/ and web.py.

Goal: the second half of the UI.

Build:
- the Results view (§20.2 item 5): the list, a result's page with its flags, metric cards, every chart of §20.3, the trades table, exports, rename, keep offline, delete, and the compare view;
- the Optimize view (item 6): ranges from the schemas, the live count of combinations, the estimate and its confirmation, progress, the sortable table, the heatmap, the out-of-sample and walk-forward views, and promote.

Charts: canvas renderer, richText tooltips, time axes, inside and slider zoom, range buttons, lttb sampling, the log-axis toggle on equity, green up and red down with signs and labels, and the theme switch with prefers-color-scheme. Candles load in windows of at most 5,000 bars.

Tests (AC-21): extend the locale coverage and the static checks, and check that the chart colour constants make up green and down red.

Then extend the manual checklist in the plan:
- results on each device;
- zoom by pinch, drag and the range buttons; the log axis; tooltips; the monthly heatmap;
- candles with markers, on a daily and a minute run;
- comparing 4 runs;
- an optimization with its heatmap, out-of-sample and walk-forward views;
- a CSV export that opens in Excel with Chinese names intact.

Done when: AC-21 holds, pytest passes, and the checklist is in the plan.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 19: PWA and offline viewing

```text
Read SPEC.md §19 (GET /api/offline), §20.5 and §21. Skim btlab/static/ and web.py.

Goal: saved results viewable offline.

Build:
- btlab/static/sw.js: the versioned shell cache with cleanup, stale-while-revalidate for the shell, network-first API GETs with the results-cache fallback, and no caching for any other method;
- in app.js: registration; syncing the results cache to GET /api/offline at startup, after opening a result and after a keep-offline change; the offline mode of §20.5 (the banner with the time of the last successful state, stored results only, actions disabled); logout deletes the results cache;
- sw.js is served with the right content type and the §21 headers.

Tests (AC-22):
- the shell list in sw.js equals the static files it must cache, computed from the folder;
- sw.js answers only GET requests from its caches;
- logout clears the results cache (a static check of that code path);
- the offline strings exist in both locales.

Then extend the manual checklist in the plan: on the iPhone and the iPad, from the home-screen icon, open 3 results and mark one "keep offline"; disconnect Tailscale and reopen the app: the banner shows, the 4 results open, and actions are disabled; reconnect: the app recovers by itself.

Done when: AC-22 holds, pytest passes, and the checklist is in the plan.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 20: spylead import

```text
Read SPEC.md §5.6, §5.7, §8.5, §9.6 (series operands), §10.11 (IN_SAMPLE_SERIES) and §17.2. Skim http.py, jobs.py, indicators.py, rules.py and plugins.py.

Goal: spylead's series as external indicators.

Build:
- btlab/spylead_client.py: the calls of §5.6, with the session cookie held in memory and the request rules of §5.7.
- The SPYLEAD_SNAPSHOT job on the DATA lane; a snapshot after every data update when spylead is configured; replacement of the stored set in one transaction; SPYLEAD_UNREACHABLE.
- Registration of the stored series in the registry (§8.6), alignment by calendar day, bars.ext() for plugins, and IN_SAMPLE_SERIES for F and COMPOSITE in backtests and optimizations.
- GET /api/spylead, POST /api/spylead/snapshot, and the spylead part of the state document.
- UI: the snapshot and its button in the Data view, the series in the rule builder's operand picker, and the IN_SAMPLE_SERIES banner.

Tests (AC-23), with an aiohttp test server that mimics spylead's API: the calls, the snapshot transaction, an unreachable spylead, the alignment, and the flag through a rule and through bars.ext().

Done when: AC-23 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 21: Minute bars through IB

```text
Read SPEC.md §4.2, §5.4, §5.5, §6.6, §15.1 (the boundary), §17.1 and §17.3. Skim jobs.py, store.py and calendar.py.

Goal: 1-minute bars from IB.

Build:
- btlab/ib_link.py: the HistoryPort protocol and its ib_async implementation:
  - connect with the configured client id (never 17), read-only in test mode, with a 30 s startup timeout;
  - contract resolution and caching (§5.4);
  - one historical request per symbol and session, with pacing and the pacing retry;
  - permission detection, and disconnection.
  - Verify every ib_async name against the installed 2.1.0 source. No other module imports ib_async, except ib_orders.py (prompt 23).
- The MINUTE lane and the MINUTE_BACKFILL job (§17.3): the queue built from coverage, newest first; normalization to raw prices (§5.4); coverage states; the pause while DATA runs; cancel; IB_UNREACHABLE; the follow-up after updates.
- POST /api/minute/backfill, the IB part of the state document, minute coverage in GET /api/symbols.
- UI: the minute toggle, coverage and backfill progress in the Data view, and the NO_PERMISSION message.

Tests (AC-24), with a fake HistoryPort and the injected clock:
- the request parameters, at least 2 s between requests, the pacing retry;
- normalization: a factor near 1, near the split product, and a mismatch;
- coverage states and resumption after a cancel;
- NO_PERMISSION stops the market;
- a read-only connect in test mode with the configured client id;
- a scan of the source tree: only ib_link.py and ib_orders.py import ib_async, and no test imports it.

Done when: AC-24 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 22: Live portfolios and test mode

```text
Read SPEC.md §14, §15.1, §15.2 (lots), §17.1, §17.2 (follow-ups), §17.4 (cutoffs), §17.6 and §19 (live routes). Skim backtest.py, engine.py, jobs.py and store.py.

Goal: live portfolios with virtual orders and the paper-trading curve.

Build:
- btlab/live.py, pure: replay targets including pending orders, target weights, live equity, live lots, lines and skips, batch replacement, cutoffs, virtual fills, corporate actions on the virtual book, and ledger rows (§14.2 to §14.6).
- The LIVE lane with LIVE_GENERATE and LIVE_VIRTUAL_FILL; the update follow-ups and LIVE_DATA_INCOMPLETE (§17.2); expiry at the cutoffs through the scheduler (§17.4); the BATCH_READY email.
- Store access for portfolios, batches, lines, holdings and the ledger.
- Routes: GET, POST and DELETE /api/live, the curve, batches, regenerate, and confirm. The confirm route dispatches by run mode: the test-mode branch is built here; the trade-mode branch comes in prompt 23 and until then answers 409 NOT_CONNECTED.
- The live part of the state document.
- UI: the Live view (§20.2 item 7) for test mode: create a portfolio, holdings, the curve, the current batch with checkboxes and its confirmation dialog, and the batch history.

Tests (AC-25, and the live part of AC-29), with temporary databases, synthetic data and the injected clock:
- creation rules; replay targets with a pending order; live lots; each skip; replacement and expiry;
- virtual fills: MOO, limit buys and sells, an untradable and a locked bar, costs;
- a split and a dividend on the virtual book;
- ledger rows and the three curves;
- test mode never constructs the order adapter.

Done when: AC-25 holds and pytest passes.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 23: IB orders and ibguard coordination

```text
Read SPEC.md §14, §15, §16, §17.6, §19 (live and ibguard routes), §20.2 item 7 and §21. Skim live.py, jobs.py, ib_link.py, web.py and __main__.py.

Goal: trade mode and the coordination with ibguard.

Build:
- btlab/ib_orders.py: the ib_async implementation of the OrderPort protocol: place (MOO and LIMIT with the fields of §15.4), cancel own orders, this client's open orders, completed orders, executions and commission reports, and order events. It is the only module that calls placeOrder or cancelOrder.
- btlab/trading.py:
  - limits (§15.3), contract sizes and market-rule rounding (§15.2);
  - the send function and its refusals, reducing lines first, BATCH_STALE, line states and ledger fills (§15.5);
  - monitoring until 15 minutes after the session (§15.6), recovery by exact orderRef (§15.7), cancelling btlab's own working orders (§15.8), READ_ONLY_API and INTERNAL_ERROR (§15.9).
- btlab/ibguard_client.py and the protocol of §16.2 with its exclusion rows, restoration, sweeps and retries; the threshold preview, token and write of §16.3.
- Wiring by run mode in __main__.py: test mode never constructs the order adapter (§15.1).
- The LIVE_SEND, LIVE_CANCEL and LIVE_SWEEP jobs, and the emails BATCH_DONE, PROTECTION_RESTORED, IBGUARD_RESTORE_FAILED and READ_ONLY_API.
- Routes: the trade-mode branch of confirm, cancel, and the ibguard preview and write; the limits and services parts of the state document.
- UI: the Live view's trade-mode parts (account kind, the red live banner, the confirmation dialog with the ibguard exclusions it will make, cancel) and the ibguard panel.

Tests (AC-26, AC-27), with a fake OrderPort, a fake account port, and an aiohttp server that mimics ibguard's API, plus a scan of the source tree for placeOrder, cancelOrder and reqGlobalCancel.

Then write into the plan, step by step, the paper-account checklist of SPEC.md §24.3 item 7.

Done when: AC-26 and AC-27 hold, pytest passes, and the checklist is in the plan.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 24: Operations and README

```text
Read SPEC.md §23, §24.3, §25 and §27. Skim __main__.py, jobs.py, trading.py and bench.py.

Goal: make it operable.

Build:
- scripts/install-autostart.ps1, with -Remove, as §23 describes: the per-user logon task, the launcher in %LOCALAPPDATA%\btlab that waits for the project folder, and the Startup-folder fallback. It must work in Windows PowerShell 5.1.
- run.bat, which starts the app from the venv with config.toml.
- Check startup, shutdown, logging, the lock and keep-awake against §23, and fix any gap.
- README.md for the owner:
  - what btlab does and does not do, in plain words;
  - prerequisites; getting the Tiingo, FinMind and FRED keys and the spylead and ibguard tokens;
  - install from pylock.toml, configure, and run for the first time;
  - lists, the first update and how long it takes (Tiingo's hourly limit), and minute backfill;
  - backtests: price modes, fill timing, intrabar paths, exits, costs and taxes, and every flag;
  - plugins in both styles, with the examples, and the rule builder;
  - optimization and its traps;
  - live portfolios: test mode, trade mode, limits, the ibguard coordination and threshold writing;
  - the PC: Edge, "Install this site as an app";
  - iPhone and iPad: Tailscale Serve on port 10000, the home-screen icon, offline viewing, and why Funnel or a public port must never be used;
  - IB Gateway settings, and that IB does not offer Taiwan stocks to residents of Taiwan;
  - autostart, the external drive's fixed letter, and backups;
  - the bench and the checks of §24.3;
  - the limitations of §25 in plain words.

Tests:
- what can be checked from Python without running install-autostart.ps1: it exists, names the task btlab, writes the launcher under %LOCALAPPDATA%\btlab, waits for the folder, and has a -Remove path and a Startup-folder fallback;
- AC-28 still holds.

Done when: pytest passes, and the README takes me from a clean PC to a first backtest and a test-mode live portfolio without asking you anything.

Plan first in the format CLAUDE.md requires, then wait for my approval.
```

## Prompt 25: Final audit

```text
Read SPEC.md in full, then the whole codebase and test suite.

Goal: an audit. No new features.

Deliver:
1. A table of every acceptance criterion in SPEC.md §24.2: the tests that prove each one, or the manual step, or "gap".
2. Every place the code differs from SPEC.md, each marked as a code defect or a spec ambiguity.
3. Every path by which a backtest, an optimization or a live batch could use data from after the bar it acts on, or the evidence that there is none.
4. Every path by which btlab could send, modify or cancel an order it should not (test mode, foreign orders, limits, ibguard races, duplicate sends), or the evidence that there is none.
5. Every place where a key, a token, the session value or the SMTP password could reach a log, an event, a response, an email or an error, or the evidence that there is none.
6. Dead code, unused dependencies, and anything that breaks a rule in CLAUDE.md.
7. Proposed changes to TODO.md: finished items to remove, and new deferred items to add.

Do not change code yet. After I review the report, fix the gaps I approve, one small diff at a time.
```
