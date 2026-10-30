# Prompt 13: Optimization and bench

Covers SPEC.md §9.7, §13 and §24.3. Acceptance: AC-16, AC-17 and the optimization part of AC-29.

## 1. Steps

1. **`btlab/optimize.py`** (orchestration; the pool is injected):
   - **The request (§13.1).** `parse_request(raw, plugins) -> Request`: `config` (a run configuration, §10.1), `ranges` keyed by JSON path in the order given, `objective` (SHARPE by default, CAGR, SORTINO, CALMAR, PROFIT_FACTOR), `min_trades` (20), `mode` (GRID, OOS, WFO) and `confirm_long`. A range is `{min, max, step}` or `{values: [...]}`; a path must name a field of the validated base configuration (`strategy.params.fast`, `exits.ibguard.tp_pct`, `sizing.risk_pct`). Field errors raise `RunConfigError` (INVALID_FIELD) with their paths; more than 2,000 combinations raise `OptimizeError("TOO_MANY_COMBOS")` before any combination is built.
   - **Combinations (§13.2).** Enumerated lexicographically in the order of the ranges (the last range varies fastest). Each is the base document with its values set, validated as a run configuration with its picklable strategy; one that fails validation (for example `fast` not below `slow`) is kept with its error and never runs. When none is valid the request is an INVALID_FIELD error.
   - **The estimate (§13.6).** The first valid combination runs once on the full data, timed by the injected clock: `estimate = its time × runs / workers`, where runs counts the valid combinations (× windows for WFO). Above 10 minutes without `confirm_long`, `optimize` returns `NeedsConfirmation(estimate_seconds)`, which the job layer turns into 409.
   - **The pool (§13.2, §13.7).** `optimize` opens the injected pool factory with a top-level initializer that receives the loaded `RunData` and the tax table once per worker; each task (`Task`: configuration, strategy, window rows, opening capital, close at the end) runs `run_task`, a top-level function that returns the run's metrics and flags, and its curve and trades when asked. `process_pool(workers)` makes the real `ProcessPoolExecutor` with the spawn start method that Windows uses; `default_workers()` is `max(1, os.cpu_count() − 1)`.
   - **Best (§13.2).** The highest objective among the combinations with at least `min_trades` closed trades and a defined objective; a tie goes to the earlier combination.
   - **GRID** runs every combination on the whole period.
   - **OOS (§13.4).** The split bar is the period's first bar plus `floor(0.7 × bars)`; the grid runs on the first part and the best combination on the rest; the report holds the split day and the best's in-sample and out-of-sample metrics.
   - **WFO (§13.5).** Window k trains on `[s_k, s_k + 3 years)` and tests on `[s_k + 3 years, s_k + 4 years)`, `s_(k+1) = s_k + 1 year`, in calendar offsets (29 February becomes 28 February); a last test window shorter than 3 months is dropped. Each window's best runs on its test period, starting flat with the previous window's final equity and closing its positions at its last close with costs; a window where no combination qualifies is NO_RESULT and its test period runs in cash. The report holds, per window, the periods, the chosen values, the training objective, the test metrics and each combination's objective and trades, then the chained curve and its metrics.
   - **Window runs (§13.4).** `backtest.window(config, data, first, stop, capital)`: the 300 rows before the window serve as warm-up, equity starts at the initial capital (or the given capital) on its first bar, and the data end with the window. A minute run's window needs minute coverage for every symbol (NO_MINUTE_DATA otherwise).
   - **Progress and cancellation (§13.6).** `progress(phase, done, total)` after each run (phases ESTIMATE, GRID, OOS, WFO); `cancelled()` is checked between runs. A cancel cancels the queued runs and returns the results so far with status CANCELLED.
   - **The look-ahead check (§9.7).** Grid runs skip it; it runs in the parent for the first combination and for the best one (each window's chosen combination in WFO). LOOKAHEAD_SUSPECT and every other flag of the runs (so IN_SAMPLE_SERIES once prompt 20 sets it) are merged into the optimization's flags, each with the detail of its first occurrence.
   - **Results.** `Optimization(status, results, best, flags)`: `results` holds the paths, every combination's values with its metrics or its error, and the OOS or WFO report; `best` holds the index, values and objective (null for WFO, whose choices are per window).
   - **For the API (prompt 16):** `heatmap(document, results, x, y)` (§13.3): the objective over two paths, the other values fixed at the best combination's, null below `min_trades`; `combination_config(document, values)`: the run configuration a promotion runs (§13.7).
2. **`btlab/backtest.py`.** `run_backtest(..., check_lookahead=True, flatten=False)`: grid runs skip the check, and `flatten` closes every position at the last bar's close through the engine's close-out (§13.5). `lookahead(config, data, strategy)` runs the check alone. `BacktestResult.curve` keeps the strategy's curve (days and DTB3 rates), which the chained curve needs. `window(...)` as above.
3. **`btlab/builtins.py`.** `BuiltinStrategy(id, params)`, a picklable strategy like `RuleStrategy` and `PluginStrategy`, replaces the lambda of `strategy_factory`, so every strategy can travel to a pool worker.
4. **`btlab/store.py`.** `save_optimization(created_at, name, market, mode, status, config, results, best, flags) -> id`, `optimization(id)`, `optimizations(market=None)` (newest first, without the results) and `delete_optimization(id)`; `OptimizationRow` in `records.py`.
5. **`btlab/bench.py`** (§24.3): `daily_data(symbols, years)` (synthetic walks with quarterly dividends and splits) and `minute_data(symbols, years)` (1-minute US bars with matching daily bars) build `RunData` in memory, without a store; the three cases time one daily run (50 × 30 years, MA_CROSS, ibguard exit, REALISTIC), a 2,000-combination grid of it with the real pool, and one 1-minute run (10 symbols × 3 years, a rule with daily operands and close-out). `python -m btlab.bench` prints each case's time. Not run here.
6. **Tests** (`tests/test_optimize.py`, `tests/test_bench.py`; the in-process pool stand-in goes to `tests/fakes.py`):
   - ranges, paths and their errors; lexicographic order; 2,000 pass and 2,001 raise TOO_MANY_COMBOS; invalid combinations kept with their errors;
   - `min_trades`, an undefined objective, ties to the earlier combination;
   - the heatmap with the other values fixed at the best;
   - the 70% split and the warm-up of window runs (a window run equals an ordinary backtest of that period);
   - walk-forward windows (3, 1 and 1 years, the dropped short window), NO_RESULT in cash, the chained curve starting each window at the previous final equity and flat at each window's end;
   - the estimate and the 10-minute threshold with an injected clock;
   - a cancel keeps the partial results;
   - LOOKAHEAD_SUSPECT for a peeking plugin, and merged flags;
   - identical results with the in-process stand-in and with a real pool of 2 workers on a tiny case, and twice in a row (AC-29);
   - the store's optimization functions;
   - `bench.py` imports and builds its data at a tiny size.
7. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **Invalid combinations** (a value outside a parameter's range, `fast` not below `slow`) are reported in the results with their errors and never run; they count toward the 2,000.
- **An undefined objective** (null, such as a profit factor without losing trades) does not qualify for best.
- **"bars" of the 70% split** are the period's trading days on both timelines; on minute timelines a window holds whole sessions.
- **The warm-up of a window run** is the same 300 bars as an ordinary backtest's, so a window run equals a backtest of that period.
- **The heatmap without a best** fixes the other values at the first combination's; every cell is then null anyway.
- **A period too short** for OOS (fewer than 2 bars on a side) or WFO (no window) fails the optimization with PERIOD_TOO_SHORT.

## 2. Extension points

- **An objective** is one entry of `OBJECTIVES`.
- **A mode** is one function from the search to its report.
- **The pool** is any factory of `concurrent.futures` executors that takes an initializer.

## 3. Refactor seams

- Workers only ever see `Task`s and the initializer's data; everything else stays in the parent.
- Every run of an optimization is an ordinary `run_backtest`, on whole or windowed data.
- Combination results are stored by index, so completion order never matters.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-16 and AC-17 hold, and the optimization part of AC-29.** `.venv/bin/python -m pytest -q`: 1008 passed (also with `-X dev -W error`, which the spawned workers of the real-pool test inherit).
- Built as planned. Every strategy is now picklable (`BuiltinStrategy`, `RuleStrategy`, `PluginStrategy`), so tasks carry their strategy to the workers.
- OOS is one train/test window and WFO a list of them; both, and GRID, are validated against the loaded period before the estimate runs.
- Scaled-down timings of the bench's own functions here (not `python -m btlab.bench`): one daily run of 50 symbols × 10 years took 0.8 s (so about 2.5 s for 30 years, target 5 s); one 1-minute run of 10 symbols × 3 months took 1.5 s (so about 18 s for 3 years, target 60 s). Case 2 depends on the PC's cores: about 2,000 × 2.5 s / workers.
- **For prompts 14 and 16:** the job runs `parse_request` (400 INVALID_FIELD or TOO_MANY_COMBOS), loads with `load_run(store, minute_store, request.config)`, then `optimize(request, data, taxes, process_pool(default_workers()), default_workers(), progress=..., cancelled=...)`; a `NeedsConfirmation` becomes 409 with `estimate_seconds`, and an `Optimization` is saved with `store.save_optimization(..., request.document, ...)`. `heatmap(row.config, row.results, x, y)` and `combination_config(row.config["config"], list(row.config["ranges"]), values)` serve the heatmap and promote routes. OptimizeError PERIOD_TOO_SHORT fails the job with its message.
