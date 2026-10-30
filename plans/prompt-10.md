# Prompt 10: Plugins and the look-ahead check

Covers SPEC.md §9.1, §9.2, §9.5 and §9.7. Acceptance: AC-13.

## 1. Steps

1. **`btlab/plugins.py`**:
   - **Discovery and loading.** `load_plugins(folder) -> (plugins, errors)` loads every `*.py` file of `strategies/` whose name does not start with `_`, in name order, each as its own module (`importlib` from the file path, registered under `btlab_plugins.<stem>`). The id is the file's stem. A file that fails is listed as `LoadError(id, path, error)` and the others still load (§9.5). Reload is the same call again.
   - **The module contract** (§9.5): `NAME_EN` and `NAME_ZH` (non-empty strings), `TIMEFRAME` (`1d`, `1m`, `5m`, `15m`, `30m` or `60m`), `PARAMS` (§9.2, through `params.parse_schema`), and exactly one of `signals(bars, params)` and `on_bar(ctx)`; `OUTPUT` (`"weights"`, the default, or `"direction"`) for `signals` only. Errors name the problem: a syntax error with its line, an exception while importing, a missing or wrong attribute, invalid `PARAMS`, both functions or neither.
   - `Plugin(id, path, name_en, name_zh, timeframe, params, style, output, module)`, style `vector` or `event`, output WEIGHTS, DIRECTION or ORDERS.
   - **`Bars`**, what `signals` receives: read-only views (`writeable = False`) of `symbols`, `times`, `session`, `open`, `high`, `low`, `close`, `volume` (adjusted), `tradable`, `rf`, `daily` (None on the daily timeline; prompt 11 fills it) and `ext(name)` (prompt 20 registers the series; an unknown name raises `KeyError`).
   - **`Context`**, what `on_bar` receives at bar t: `t`, `time`, `symbols`, `params` (a read-only mapping), `state` (one dict for the whole run), `history(field, n)` (the last n rows up to and including t, read-only), `position(symbol)`, `equity`, `cash`, and the orders `order_target_weight`, `order_target_qty`, `order_qty`, `order_value` by symbol name, through the engine's `Book` (the last order for a symbol in a bar wins). It has no other attribute: no bars beyond t, no store, config, IB or network object.
   - **`PluginStrategy(path, params)`**, the run's strategy factory: callable with a `View`, it loads the module from `path` once per process (pool workers, prompt 13, load it themselves) and returns `Targets` from `signals` (shape and mask checked; a wrong shape raises `PluginError` naming it) or an event strategy over `on_bar`.
   - **The look-ahead check** (§9.7): `lookahead(view) -> detail | None` on a `PluginStrategy`, and the plain function `lookahead_check(signals, view, params)` for the optimizer (prompt 13). It computes the targets on the full data and on the data truncated after the bars at 50, 60, 70, 80 and 90% of the run (from its first trading bar), and compares every bar up to each cut, NaN equal to NaN, within 1e-9, rebalance masks included. The detail of LOOKAHEAD_SUSPECT is the earliest differing bar's `time` and its first differing `symbol`. Event plugins need no check.
2. **`btlab/builtins.py`**: `View` gains `tradable`, which `Bars` exposes.
3. **`btlab/backtest.py`**:
   - `STRATEGY_SOURCES` adds `plugin`; `validate_config(raw, plugins=None)` checks a plugin's id (it must have loaded), its parameters against its `PARAMS`, and that its `TIMEFRAME` is the run's timeframe.
   - `strategy_factory(config, plugins=None)` returns a `PluginStrategy` for a plugin; strategy factories now take the `View` (the strategies' input) instead of `RunData`.
   - `run_backtest` runs the look-ahead check of a vectorized plugin first and sets LOOKAHEAD_SUSPECT with its detail; the backtest still runs (§9.7).
4. **`strategies/example_vector.py`** (an MA cross returning equal weights over the symbols above their slow average) and **`strategies/example_event.py`** (a breakout through `on_bar`), small, commented only where a reason helps.
5. **Tests** (`tests/test_plugins.py`, AC-13), with plugins written to temporary folders:
   - both styles load; the two committed examples load and run through `run_backtest` from a temporary store;
   - each broken plugin is listed with its error while the others load: a syntax error, an import that raises, a missing name, a bad `TIMEFRAME`, invalid `PARAMS`, both functions, neither, a bad `OUTPUT`; files starting with `_` are skipped;
   - `PARAMS` validation through `validate_config`, with paths; a plugin whose timeframe is not the run's;
   - `Context.history` returns only rows up to t, and writing to a `Bars` or `Context` view raises; `Context` has exactly the documented attributes and `Bars` exactly its fields;
   - a plugin that reads one bar ahead is flagged with the first bar and symbol that differ; an honest one is not; the flag travels with the result;
   - `signals` returning a wrong shape raises `PluginError`; a rebalance mask is passed through.
6. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **"50 to 90% of the run"** counts the run's trading bars, from its first bar after the warm-up.
- **A plugin whose `TIMEFRAME` differs from the run's** is an INVALID_FIELD at `timeframe`.
- **An exception inside a plugin** during a run fails that run with the plugin's error; it is the owner's code (§9.5) and the backtest cannot continue without its targets.

## 2. Extension points

- **A field of `bars`** is one attribute of `Bars` (prompt 11 fills `daily`, prompt 20 `ext`).
- **An order function of `ctx`** is one method over `Book`.

## 3. Refactor seams

- Loading, the module contract, `Bars`, `Context` and the check are separate; `PluginStrategy` only connects them to a run.
- `PluginStrategy` holds a path and parameters, so a pool worker can rebuild it without pickling a module.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-13 holds.** `.venv/bin/python -m pytest -q`: 840 passed (also with `-X dev -W error`).
- Built as planned. `PluginStrategy` keeps the loaded plugin for runs in this process and pickles as its path and parameters, so a pool worker (prompt 13) loads the file itself.
- Strategy factories now take the run's `View` (`backtest.view(data)`), and `View` carries the tradable flags that `bars.tradable` exposes.
- The test builders gained `synthetic_store`, shared by the backtest and plugin tests.
- **For prompts 11, 13 and 20:** `Bars(view, times, session, daily, series)` takes the minute timeline's times and sessions, the daily bars of §8.4 and the external series; `lookahead_check(signals, output, view, params)` is the optimizer's entry point.
