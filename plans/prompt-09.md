# Prompt 9: Built-in strategies

Covers SPEC.md §9.1 to §9.4. Acceptance: AC-12.

## 1. Steps

1. **`btlab/params.py`** (pure), the parameter schema of §9.2, shared by built-ins, plugins (prompt 10) and rules (prompt 12). A module of its own because all three use it and none of them should import another.
   - `Param(type, default, min=None, max=None, step=None, choices=(), label_en="", label_zh="")`, type `int`, `float`, `bool` or `choice`; `document()` gives its JSON for the API and the UI forms (§9.2: forms and optimization ranges read min, max and step).
   - `validate_params(schema, values, path) -> (params, errors)`: every parameter with its default filled in; an unknown name, a wrong type (booleans are not numbers, an int parameter takes whole numbers only, numbers must be finite) or a value outside [min, max] or `choices` is an error `(path, message)`, as INVALID_FIELD (§9.2). `step` guides the UI and optimization ranges and does not restrict values.
   - `parse_schema(raw) -> dict[str, Param]`: a schema written as data (a plugin's `PARAMS`, a rule's `params`), with every problem named; its default must satisfy it.
2. **`btlab/builtins.py`** (pure), strategies A to J of §9.3:
   - `View(symbols, days, bars, rf, start)`: what a built-in reads, the run's adjusted bars (`indicators.Inputs`) from the warm-up on (§10.2), the DTB3 rate per bar and the first trading bar.
   - `Builtin(id, name_en, name_zh, output, schema, check, run)`, where `schema(market, symbols)` gives the parameter schema (DCA's amount depends on the market and DUAL_MOMENTUM's `safe` on the run's symbols), `check(params)` the rules that join parameters (MA_CROSS and MACD: `fast` below `slow`), and `run(view, params)` the targets. `BUILTINS` maps each id to its entry.
   - A MA_CROSS, C MACD: +1 while the fast line is above the slow one; otherwise −1 when the strategy's `allow_short` is set, else 0. NaN (no instruction) while an input is NaN.
   - B RSI, D BOLLINGER, E BREAKOUT, J ZSCORE_REVERSION: one state machine per symbol over the rule's entry and exit conditions; an exit and an entry of the other side on the same bar reverse the position (§9.3). NaN while the rule's indicators are NaN, so the engine keeps the position.
   - F BUY_HOLD: on the run's first bar, equal weights over the symbols that have data; then nothing.
   - G DCA (ORDERS, through the engine's `Book`): on the first bar of each period, `amount / N` for each of the N symbols with data, in symbol order, while the cash covers each buy with slippage and commission (`Book.buy_cost`, new); never sells.
   - H MOMENTUM_ROTATION: on the first bar of each period, score = ROC(lookback) at bar t − skip; the top_k symbols with a score (above 0 when `abs_filter`), ties to the earlier symbol; equal weights 1/top_k, or vol_inverse weights ∝ 1/VOLATILITY(60) scaled to selected/top_k (AC-12); the rest in cash. Rebalance mask: the period starts.
   - I DUAL_MOMENTUM: on the first bar of each month, the risk symbol (all but `safe`) with the highest ROC(lookback); 100% in it when that ROC is above 0 and above the compounded risk-free return over the same bars (Π(1 + DTB3/100/252) − 1), else 100% in `safe`, or cash when `safe` is empty.
   - Periods come from calendar.py's `period_starts` (§9.3: the first bar whose date falls in a new month or ISO week).
3. **`btlab/engine.py`**: `Book.buy_cost(j, value)`, the cash a buy of about `value` needs at this close with slippage and commission.
4. **`btlab/backtest.py`**: `validate_config` checks a `builtin` strategy's id and parameters (paths `strategy.id`, `strategy.params.<name>`) and stores the complete parameters; `strategy_factory(config)` turns a validated configuration into the factory `run_backtest` takes (`builtin` here; plugins and rules in prompts 10 and 12).
5. **Tests:**
   - `tests/test_params.py`: types, ranges, choices, defaults, unknown names, booleans as numbers, and schema parsing errors.
   - `tests/test_builtins.py` (AC-12), on synthetic series built to trigger each rule: entries, exits and reversals of A to E and J, with and without `allow_short`; BUY_HOLD; DCA's period starts and its running out of cash; MOMENTUM_ROTATION's period starts, `skip`, `abs_filter`, top_k with fewer candidates and vol-inverse weights summing to selected/top_k; DUAL_MOMENTUM's risk symbol, safe asset and cash, against the risk-free return; truncation invariance of every built-in's targets (DCA through its fills); parameter validation errors through `validate_config`; every sizing mode through the engine with MA_CROSS, the held quantity and the `max_position_pct` cap; rotations never short.
6. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **Vol-inverse weights sum to selected/top_k**, as §9.3 and AC-12 say; §9.4's "sum to 100% over the selected symbols" is the case of a full selection. A selected symbol without a positive finite volatility gets no weight and does not count as selected.
- **MACD's `fast` must be below `slow`**, like MA_CROSS's. Unstated ranges: MACD `fast` 2 to 100, `slow` 3 to 200, `signal` 2 to 100; DCA `amount` above 0; `top_k` 1 to 20 as given.
- **DUAL_MOMENTUM's risk-free return** compounds DTB3 per bar with 252 bars a year, the daily timeline's P of VOLATILITY (§8.1).
- **DCA spends in symbol order** and stops at the first buy the cash cannot cover.
- **A strategy's `allow_short`** says whether it emits shorts; the run's `allow_short` says whether the engine takes them (otherwise SHORTS_IGNORED, §9.1).

## 2. Extension points

- **A built-in** is one function and one `BUILTINS` entry with its schema; the API (prompt 15) lists `BUILTINS` with `Param.document()`.
- **A parameter type** is one branch in `params.check_value` and `parse_schema`.

## 3. Refactor seams

- Built-ins read a `View`, not the store or `RunData`, so the truncation tests and plugins' `bars` (prompt 10) share its shape.
- State machines, period starts and the rotations' selection are separate helpers.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-12 holds.** `.venv/bin/python -m pytest -q`: 822 passed (also with `-X dev -W error`). Every built-in's targets pass the truncation test exactly; DCA passes it through its fills.
- Built as planned. `validate_config` accepts only `builtin` strategies for now (`STRATEGY_SOURCES`); prompts 10 and 12 add `plugin` and `rule` with their validation and their branch of `strategy_factory`.
- A built-in's parameters are stored complete in the configuration, defaults included, so a saved run replays exactly.
- **For prompt 10:** `params.parse_schema` reads a plugin's `PARAMS`; `builtins.View` is the shape the plugin `bars` object can wrap (adjusted `Inputs`, days, DTB3, first trading bar); DCA shows an ORDERS strategy over `engine.Book`.
