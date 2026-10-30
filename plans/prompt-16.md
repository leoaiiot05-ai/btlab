# Prompt 16: Web API for results and optimization

Covers SPEC.md §10.11, §12, §13, §19 and §20.5 (the server's part). Acceptance: AC-20, the API part of AC-22, and AC-30.

## 1. Steps

1. **Payloads (§19)** in `web.py`: times are day integers on the daily timeline and UTC epoch seconds on minute ones, with `time_unit`; data values (series, candles, metrics, tables, trades, heatmaps, optimization results) are rounded to 6 significant digits, NaN and infinities becoming null. Configurations, rules, flags and parameter values come back exactly as stored.
2. **Backtests:**
   - `POST /api/backtests` `{"config"}`: 202 `{"job"}`; 400 INVALID_FIELD with the first error's path and every error in `errors`; 409 JOB_RUNNING.
   - `GET /api/backtests?market=&limit=`: newest first (limit 100 by default, at most 1,000): id, name, created, market, timeframe, strategy, period, key metrics (total return, CAGR, Sharpe, max drawdown, trades), flag names, keep_offline.
   - `GET /api/backtests/{id}`: configuration, data stamp, metrics, tables and flags; it sets `opened_at`.
   - `GET /api/backtests/{id}/series`: the series payload of EQUITY, BENCHMARK, CASH, GROSS, NET, POSITIONS and DRAWDOWN.
   - `GET /api/backtests/{id}/trades?offset=&limit=`: the page of trades and the total.
   - `GET /api/backtests/{id}/candles/{symbol}?from=&to=`: the run's symbol on the run's timeframe, raw in REALISTIC mode and adjusted in ADJUSTED mode, so the trade markers (entries, and exits other than OPEN) sit on the bars.
   - `GET /api/backtests/{id}/export/trades.csv` and `equity.csv` (§12, UTF-8 with a BOM).
   - `PATCH /api/backtests/{id}` `{"name"?, "keep_offline"?}`; `DELETE /api/backtests/{id}` (409 IN_USE while an active live portfolio uses it).
   - `GET /api/offline` (§20.5): `{"ids"}`, the 20 most recently opened results plus every keep-offline one.
   - `GET /api/compare?ids=`: 2 to 4 results (400 otherwise), each curve divided by its first value, aligned on the union of their days (a minute run's session ends fall on their days), null where a run has no point, and the metrics side by side.
3. **Candles of stored symbols:** `GET /api/candles/{market}/{symbol}?tf=&from=&to=&adjusted=`: daily bars, or 1-minute bars resampled to the timeframe per session (§11.1); adjusted (the default) through the symbol's own adjustment (§6.3), or raw; `from` and `to` in the payload's time unit; at most 5,000 bars, the last ones of the window. Without `from`, a minute window starts 40 days before `to`.
4. **Optimizations:**
   - `POST /api/optimizations`: 400 INVALID_FIELD or TOO_MANY_COMBOS before any job; then the job's estimate decides: 409 NEEDS_CONFIRMATION `{"estimate_seconds"}`, or 202 `{"job"}`; 409 JOB_RUNNING.
   - `GET /api/optimizations` (newest first, without results), `GET /api/optimizations/{id}` (the request, results, best and flags).
   - `GET /api/optimizations/{id}/heatmap?x=&y=` (§13.3); 400 INVALID_FIELD for paths that are not two of the optimization's.
   - `POST /api/optimizations/{id}/promote` `{"combo"}`: the combination's values, one per range path; it starts an ordinary BACKTEST job (§13.7), named after the optimization and its values.
   - `DELETE /api/optimizations/{id}`.
5. **`btlab/store.py`:** `backtests(market, limit)` (without tables), `open_backtest(id, at)`, `update_backtest(id, name, keep_offline)`, `delete_backtest(id)` (series and trades go with it), `offline_backtest_ids(recent)`, `backtest_in_use(id)` and `backtest_trade_count(id)`.
6. **Tests** (`tests/test_web_results.py`), with the real jobs over the in-process pool and temporary stores:
   - every route's success and error paths (404 for unknown ids, 400 for bad parameters, 409 cases);
   - series and candle payloads: time units, rounding to 6 significant digits, null for NaN, the 5,000-bar window, adjusted and raw, minute resampling, trade markers;
   - both CSV routes;
   - compare with 2 and 4 ids, and 400 with 1 and 5;
   - `GET /api/offline` and `opened_at`;
   - AC-30: SURVIVORSHIP, LOOKAHEAD_SUSPECT, SHORTS_IGNORED, PATH_FALLBACK, DATA_ENDED, BENCHMARK_PARTIAL and PDT each arise in a scenario, are stored, and are served with their result.
7. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **IN_SAMPLE_SERIES** cannot arise before spylead's series exist; prompt 20 adds its scenario (AC-23 names it too).
- **"At most 5,000 bars"** returns the last 5,000 of the requested window; the UI asks for earlier windows with `to`.
- **A promoted combination** is given as its values, so a walk-forward window's choice can be promoted as well as a grid row.
- **The backtest list's period** is the configuration's start and end (null for "the latest bar").

## 2. Extension points

- **A payload field** is one entry of its builder; the rounding helper applies to any data payload.
- **A CSV export** is one route over `backtest.py`'s writers.

## 3. Refactor seams

- Candle building reads the stores and uses `prices.align` and `intraday.resample`; the routes only parse windows.
- Optimization routes call `optimize.heatmap` and `optimize.combination_config` on the stored request.

## 4. Deferred items (appended to TODO.md)

- None.
