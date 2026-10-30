# Prompt 8: Metrics, benchmark and the backtest runner

Covers SPEC.md §5.3, §10.1, §10.9, §10.11, §12 and §17.5. Acceptance: AC-11, and the backtest part of AC-29.

## 1. Steps

1. **`btlab/records.py`**: `Trade` moves here from engine.py (the store saves it and the web layer serves it), and `BacktestRow(id, created_at, name, market, timeframe, config, data_stamp, metrics, tables, flags, keep_offline, opened_at)` with the JSON columns decoded.
2. **`btlab/metrics.py`** (pure), §12:
   - `Curve(days, equity, rf)`: equity points with each point's calendar day and DTB3 in percent. `risk_free(days, rf_days, rf_pct)` gives the latest DTB3 value on or before each day, 0 before its first value.
   - Returns `r_t = E_t / E_(t−1) − 1`; `P = returns / (calendar days from the first to the last point / 365.25)`; `rf_t = y_t / 100 / P`; `e_t = r_t − rf_t`.
   - `curve_metrics`: total return, CAGR, annual volatility (sample sd × √P), Sharpe (mean(e) / sample sd(e) × √P), Sortino (mean(e) / √mean(min(e, 0)²) × √P), max drawdown, max drawdown duration in calendar days (a peak until equity regains it, or until the end), Calmar.
   - `trade_metrics`: closed trades and open trades; win rate, payoff ratio, profit factor, expectancy in currency and as the mean trade return (closed trades only).
   - `activity_metrics`: exposure (mean gross exposure), time in market (share of points with a position), turnover (Σ |traded value| / 2 / mean equity, per year).
   - `relative_metrics`: β = cov(e_s, e_b) / var(e_b), α = (mean(e_s) − β·mean(e_b)) × P, and the correlation of r_s and r_b, over the points both curves define.
   - `drawdown(equity)` per point; `annual_returns` and `monthly_returns`, compounded per calendar year and per year × month.
   - Every undefined value (a division by zero, too few points) is None, so JSON carries null.
3. **`btlab/backtest.py`**:
   - **Validation (§10.1).** `validate_config(raw) -> RunConfig` checks every field and raises `RunConfigError` listing each INVALID_FIELD with its JSON path (`symbols[2]`, `exits.ibguard.tp_pct`). Unknown keys are invalid. Omitted optional fields take the market's defaults (§4.1: capital, benchmark, commission, slippage, withholding, borrow and short-sale fees), so the stored configuration is always complete. Numbers must be finite and not booleans. Ranges: capital above 0; `max_position_pct` in (0, 100]; sizing fields above 0 (`fraction` at most 1); exits above 0 (stop and trailing below 100, `max_hold_bars` and `atr_stop.n` whole numbers); ibguard's `tp_pct` in (0, 1000] and `sl_pct` in (0, 100) (§16.3); costs at least 0 (withholding at most 100). Symbols are normalized by §7's rules and appear once. This prompt accepts `timeframe` "1d"; prompt 11 adds the minute timeframes and `intraday`.
   - **Loading** (`load_run(store, minute_store, config) -> RunData`), the run's I/O:
     - the calendar: US the days of SPY's stored bars, TW FinMind's stored calendar; `end` null is the latest stored bar of the run's symbols; the warm-up starts 300 calendar bars before the first bar on or after `start`;
     - each symbol's bars and actions from the warm-up through the latest stored day (adjusted prices are relative to it), aligned with prices.py; the benchmark the same way;
     - DTB3, the categories and borrow-fee overrides of `symbols`, TW short-sale suspensions, and the data stamp (each symbol's latest stored bar and action);
     - with `intrabar_path` MINUTE, each symbol's 1-minute bars of the period and its days with coverage OK.
     - No bar in the period raises `RunError("NO_DATA")`.
   - **Running** (`run_backtest(config, data, taxes, strategy) -> BacktestResult`), pure given the loaded data: build the daily timeline, the cost model and the overlays (with `DailyMinutePaths` for MINUTE), simulate, then the benchmark as BUY_HOLD of its symbol at 100% through the same engine with the run's period, price mode, fill timing, costs and withholding and the same initial capital (§10.9). The benchmark's curve starts at its first bar in the period (NaN before); a benchmark missing part of the period sets BENCHMARK_PARTIAL.
     - `strategy` is a callable that receives the `RunData` and returns `Targets` or an `OrderStrategy`; built-ins arrive in prompt 9.
     - Flags: the engine's (SHORTS_IGNORED, DATA_ENDED, PATH_FALLBACK with its count), SURVIVORSHIP always, BENCHMARK_PARTIAL.
     - `BacktestResult`: the normalized configuration, `time_unit` "day", the series of §18 (`TIME`, `EQUITY`, `BENCHMARK`, `CASH`, `GROSS`, `NET`, `POSITIONS`), the trades, metrics and tables for the strategy and the benchmark, the flags and the data stamp.
   - **CSV** (§12): `trades_csv` and `equity_csv` as UTF-8 with a BOM; columns symbol, side, quantity, entry time, entry price, exit time, exit price, P&L, return %, costs, exit reason, and time, equity, benchmark, drawdown, gross exposure; times as ISO 8601 dates on the daily timeline and ISO 8601 date-times in the market's time zone on minute timelines; NaN as an empty cell.
4. **`btlab/store.py`**: `save_backtest(created_at, result) -> int` writes the row, its series (float64 BLOBs) and its trades in one transaction; `backtest(id)`, `backtest_series(id)` and `backtest_trades(id, offset=0, limit=None)` read them back. The other result routes (list, rename, delete, opened_at) come with prompt 16.
5. **Tests:**
   - `tests/test_metrics.py`, AC-11 on a small hand-made curve and trade list: every metric of §12, P from calendar days, rf forward-filled from DTB3 (0 before its first value), Sortino's downside, an unrecovered drawdown and its duration to the end, α and β against a benchmark, null for undefined values (one point, no losses, a flat curve), annual and monthly tables.
   - `tests/test_backtest.py`: each configuration error with its path, and the defaults filled in; a full run from a temporary store with synthetic data (US and TW), saved and read back, with the CSV columns and the BOM; the benchmark run, its curve and BENCHMARK_PARTIAL; SURVIVORSHIP always; two identical runs give identical results; NO_DATA.
   - `tests/test_store.py`: the results tables move from the raw round trip to `save_backtest` and its readers.
6. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **Sharpe and the deviations of α and β use the sample deviation**, like the annual volatility; β's ratio does not depend on that choice.
- **The benchmark uses the run's fill timing and lot setting**, the parts of "the same engine" §10.9 does not name.
- **`end` null is the latest stored bar among the run's symbols**; the calendar of a backtest ends there (weekdays after SPY's last bar matter only for scheduling, §4.3).
- **Expectancy** is reported as `expectancy` (currency) and `expectancy_pct` (the mean trade return in percent).

## 2. Extension points

- **A metric** is one function in metrics.py and one key in its group; **an objective** (prompt 13) reads a key.
- **A strategy source** (prompts 9, 10, 12) is a callable from `RunData` to targets.
- **A configuration field** is one entry in the validator table and one `RunConfig` field.
- **Minute timelines** (prompt 11) add a loader and a timeline builder next to the daily ones.

## 3. Refactor seams

- Loading (I/O) and running (pure) are separate functions; `RunData` holds plain arrays and records, so a pool worker (prompt 13) receives it whole.
- Metrics are pure functions of arrays and trades; the runner assembles them.
- CSV building takes a result and returns bytes; the web layer only serves them.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-11 holds, and so does the backtest part of AC-29.** `.venv/bin/python -m pytest -q`: 738 passed (also with `-X dev -W error`). The metric tests compare with values computed by Python's `statistics` module.
- Built as planned, plus `save_result(store, result, created_at)` in backtest.py, which maps a `BacktestResult` onto `Store.save_backtest`.
- `RunConfigError.errors` is a list of `(path, message)`; the web layer (prompt 15) answers INVALID_FIELD with them. `RunError.code` is NO_DATA for now; prompt 11 adds NO_MINUTE_DATA.
- Metrics are grouped as `{"strategy": {...}, "benchmark": {...}}` and tables as `{"strategy": {"annual": [...], "monthly": [...]}, "benchmark": {...}}`; flags as `{flag: detail}`.
- **For prompt 9:** `run_backtest(config, data, taxes, strategy)` takes a factory from `RunData` to `Targets` or an `OrderStrategy`; `RunData.aligned` holds the adjusted bars (`adj_*`) from the warm-up on, `RunData.rf` the DTB3 rate per bar, and `RunData.start` the first trading bar.
