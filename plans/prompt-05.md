# Prompt 5: Indicators and features

Covers SPEC.md §8. Acceptance: AC-6.

## 1. Steps

1. **`btlab/indicators.py`** (pure), every indicator of §8.1 on float64 bars × symbols arrays:
   - `Inputs(open, high, low, close, volume, periods_per_year, session=None, real=None, minutes=None)`: the adjusted bars of one timeline (§10.2). `periods_per_year` is 252 on the daily timeline and 252 × bars per regular session on minute timelines (VOLATILITY). Minute timelines (prompt 11) add `session` (the session index of each bar), `real` (the symbol has a bar at that time) and `minutes` (minutes from the session's open to each bar's end).
   - Functions: `price`, `sma`, `ema`, `rsi`, `macd` (macd, signal, hist), `bbands` (upper, middle, lower, pctb, width), `atr`, `donchian` (upper, lower, middle), `highest`, `lowest`, `roc`, `zscore`, `stdev`, `volatility`, `vwap`, with the definitions and defaults of §8.1.
   - **Causality and NaN.** A value at bar t reads bars up to t only. A window holding NaN gives NaN. The recursive ones (EMA, the MACD signal, RSI's and ATR's Wilder averages) restart after a NaN: they seed again with the mean of the next n values, so a symbol listed mid-period seeds at its own start. Non-finite results (a division by zero) become NaN.
   - **Exactness.** SMA uses prefix sums, so a window's value depends only on bars up to its end. Deviations are two-pass per window in a fixed order of addition; a window whose highest equals its lowest has a deviation of exactly 0, so ZSCORE's "NaN when sd is 0" holds. HIGHEST, LOWEST and DONCHIAN take exact maxima and minima over sliding windows. Every function is deterministic.
2. **`btlab/features.py`** (pure):
   - **§8.2 on the daily timeline.** `session_bars(market, days, bars, covered, multiplier)` cuts one symbol's 1-minute bars into the sessions of the daily timeline: only days with coverage OK, the bars between the session's open and its regular close, prices scaled by the day's `multiplier` (from `Aligned`, prompt 4), and the session's end per §4.2 (the end of the last stored bar). Then `last_n_min_return(s, n)`, `first_n_min_return(s, n)`, `close_vs_vwap(s)`, `range_position(s)`, `reclaim_prev_low(s)` return one value per day, NaN without coverage OK.
     - "The minute bar that ends n minutes before the session end" (or after the open): the last bar that ends at or before that moment, so a missing minute takes the latest earlier one; NaN when there is none.
     - RECLAIM_PREV_LOW compares with the previous day of the timeline, from its minute bars too; NaN when either day lacks coverage OK.
   - **§8.3 on minute timelines.** `today_open`, `today_high`, `today_low`, `today_vwap`, `today_return` and `minutes_since_open` from `Inputs`: each from the current session's bars up to t, counting only bars where `real` is set; NaN before a symbol's first bar of the session. MINUTES_SINCE_OPEN is measured to the bar's end, the moment the strategy evaluates it.
   - **§8.4.** `previous_session(daily_days, values, session_days)`: on a minute timeline, each bar takes the daily value of the last session before its own.
3. **`btlab/registry.py`** (pure), the registry of §8.6. A module of its own: it spans indicators.py and features.py, and holding it in either would import the other both ways.
   - `Arg(type, default, min, max)` and `Operand(name, kind, args, outputs, source, timeframes, compute)`. `kind` is the operand key of §9.6 (`ind`, `feature`, `today`, `series`); `outputs` lists the `out` choices with the default first; `source` is the default `src`, or None when the operand takes none; `timeframes` lists the timelines where it is valid (`1d` and the minute timeframes).
   - `compute(data, args)`, with `args` holding every argument plus `src` and `out` where they apply: indicators take `Inputs`, features a list of `SessionBars` (one per symbol), today-so-far values `Inputs`. External series have no compute here: their values come aligned from the store (prompt 20).
   - `operands(series=()) -> dict[str, Operand]`: every indicator, feature and today-so-far value, plus one `series` operand per external series name given (the run-time hook of §8.5).
   - Ranges: window lengths 1 to 1,000 bars (2 to 1,000 for deviations and VOLATILITY), BBANDS `k` 0.1 to 10, MACD lengths 1 to 500, LAST_N_MIN_RETURN and FIRST_N_MIN_RETURN 1 to 390 minutes.
4. **Tests** (`tests/test_indicators.py`, `tests/test_features.py`, `tests/test_registry.py`), small hand-made data:
   - every indicator against hand-computed values: SMA; EMA's SMA seed; RSI's Wilder seed and smoothing, 100 with no loss and 50 with neither; MACD's signal seeded with the SMA of its first defined values; BBANDS with the population deviation; ATR with H − L on the first bar and its Wilder seed; DONCHIAN without the current bar; HIGHEST and LOWEST with it; ROC; ZSCORE, and NaN when sd is 0; STDEV (population) against VOLATILITY (sample, × √P); VWAP per session;
   - warm-up NaN, a NaN inside a window, and recursive indicators restarting after a gap;
   - truncation invariance for every registry operand: removing the bars after bar c, or replacing them with other values, changes nothing at bars up to c, exactly;
   - §8.2 and §8.3 on hand-made minute bars, including a session without coverage, a missing minute, an early close, the multiplier, and a day after an uncovered one;
   - §8.4: on a minute timeline a daily value comes from the previous session;
   - the registry: every name of §8.1 to §8.3 with its defaults, outputs and timeframes; the series hook;
   - purity: indicators.py, features.py and registry.py join `PURE` in `tests/test_purity.py`.
5. Run `.venv/bin/python -m pytest -q` until it passes.

## 2. Extension points

- **An indicator** is one function and one `Operand` entry; validation, the rule builder and `GET /api/indicators` read the registry (§8.6).
- **A feature** is one function over `SessionBars` and one entry.
- **External series** enter through `operands(series=...)`; prompt 20 passes the stored names.

## 3. Refactor seams

- The registry describes operands and points at pure functions; rules.py (prompt 12) resolves and evaluates them, and web.py (prompt 15) serves the descriptions.
- `Inputs` is the one shape every indicator reads, on daily and minute timelines alike; minute fields are optional.
- Window sums, deviations and extremes go through three private helpers, so a faster method can replace one without touching the indicators.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-6 holds.** `.venv/bin/python -m pytest -q`: 531 passed (also with `-X dev -W error`). Every registry operand passes the truncation test exactly (bit for bit), for removed and for replaced later bars.
- `tests/test_purity.py` now parses calls instead of searching text, and covers indicators.py, features.py and registry.py.
- Timing at production size (50 symbols × 7,800 daily bars, this VM): SMA(400) 0.02 s, EMA(200) 0.04 s, RSI 0.1 s, MACD 0.14 s, HIGHEST(250) 0.07 s, STDEV(200) 0.4 s.
- **For prompt 12:** `registry.operands(series)` and `default_args`; `compute(data, args)` takes `Inputs` (indicators, today-so-far) or one `SessionBars` per symbol (features). Argument validation against `Arg` ranges is the rule builder's (BAD_ARGS).
