"""Plugins in both styles and the look-ahead check (SPEC.md §9.5, §9.7, AC-13)."""

import pickle
import textwrap
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.backtest import RunConfigError, load_run, run_backtest, strategy_factory, validate_config
from btlab.builtins import View
from btlab.engine import Targets, simulate
from btlab.indicators import Inputs
from btlab.plugins import (
    Bars,
    Context,
    PluginError,
    PluginStrategy,
    load_plugin,
    load_plugins,
    lookahead_check,
)
from builders import DAY0, TAXES, bars, settings, synthetic_store, us_costs

REPO = Path(__file__).resolve().parents[1]
HEADER = '''
NAME_EN = "Test"
NAME_ZH = "測試"
TIMEFRAME = "1d"
PARAMS = {"n": {"type": "int", "default": 3, "min": 1, "max": 50}}
'''
VECTOR = HEADER + '''
import numpy as np

def signals(bars, params):
    return np.where(bars.close > bars.close[0], 1.0, 0.0)
'''
EVENT = HEADER + '''
def on_bar(ctx):
    ctx.state["bars"] = ctx.state.get("bars", 0) + 1
    if ctx.state["bars"] == 1:
        ctx.order_target_qty(ctx.symbols[0], 10)
'''
PEEKING = HEADER + '''
import numpy as np

def signals(bars, params):
    # Tomorrow's close above today's: one bar of look-ahead.
    tomorrow = np.vstack([bars.close[1:], bars.close[-1:]])
    return np.where(tomorrow > bars.close, 1.0, 0.0)
'''
HONEST = HEADER + '''
from btlab.indicators import sma

def signals(bars, params):
    average = sma(bars.close, params["n"])
    return np.where(np.isnan(average), np.nan, np.where(bars.close > average, 1.0, 0.0))

import numpy as np
'''
BROKEN = {
    "syntax": ("def signals(bars, params)\n    return 1\n", "syntax error on line 1"),
    "raising": ("raise RuntimeError('no')\n", "RuntimeError while loading: no"),
    "nameless": (VECTOR.replace('NAME_EN = "Test"', ""), "NAME_EN must be a non-empty string"),
    "timeframe": (VECTOR.replace('TIMEFRAME = "1d"', 'TIMEFRAME = "2h"'), "TIMEFRAME must be one of"),
    "params": (VECTOR.replace('"min": 1', '"min": 10'), "PARAMS: n: the default must be at least 10"),
    "no_params": (VECTOR.replace("PARAMS = ", "PARAM_LIST = "), "PARAMS is missing"),
    "both": (VECTOR + "\ndef on_bar(ctx):\n    pass\n", "exactly one of signals(bars, params) and on_bar(ctx)"),
    "neither": (HEADER, "exactly one of signals(bars, params) and on_bar(ctx)"),
    "output": (VECTOR + '\nOUTPUT = "orders"\n', 'OUTPUT must be "weights" or "direction"'),
}


def write(folder: Path, name: str, source: str) -> Path:
    path = folder / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def view(closes, start: int = 0) -> View:
    close = np.array(closes, dtype=np.float64)
    close = close[:, None] if close.ndim == 1 else close
    days = np.arange(DAY0, DAY0 + len(close))
    return View(tuple("ABC"[: close.shape[1]]), days, Inputs(close, close, close, close, np.ones(close.shape)), np.isfinite(close),
                np.zeros(len(close)), start)


# ----- loading -----


def test_both_styles_load(tmp_path):
    write(tmp_path, "vector", VECTOR)
    write(tmp_path, "event", EVENT.replace("TIMEFRAME = \"1d\"", "TIMEFRAME = \"5m\""))
    plugins, errors = load_plugins(tmp_path)
    assert errors == []
    vector, event = plugins["vector"], plugins["event"]
    assert (vector.style, vector.output, vector.timeframe, vector.name_zh) == ("vector", "WEIGHTS", "1d", "測試")
    assert (event.style, event.output, event.timeframe) == ("event", "ORDERS", "5m")
    assert vector.params["n"].default == 3
    direction = load_plugin(write(tmp_path, "direction", VECTOR + '\nOUTPUT = "direction"\n'))
    assert direction.output == "DIRECTION"


def test_each_broken_plugin_is_listed_with_its_error_while_the_others_load(tmp_path):
    for name, (source, _) in BROKEN.items():
        write(tmp_path, name, source)
    write(tmp_path, "good", VECTOR)
    write(tmp_path, "_helper", "raise RuntimeError('a helper is not a plugin')\n")
    plugins, errors = load_plugins(tmp_path)
    assert list(plugins) == ["good"]
    found = {error.id: error.error for error in errors}
    assert set(found) == set(BROKEN)
    for name, (_, message) in BROKEN.items():
        assert message in found[name], (name, found[name])


def test_reloading_picks_up_a_changed_file(tmp_path):
    path = write(tmp_path, "plugin", VECTOR)
    assert load_plugins(tmp_path)[0]["plugin"].name_en == "Test"
    path.write_text(VECTOR.replace('NAME_EN = "Test"', 'NAME_EN = "Changed"'), encoding="utf-8")
    assert load_plugins(tmp_path)[0]["plugin"].name_en == "Changed"


def test_the_committed_examples_load_and_run(tmp_path):
    plugins, errors = load_plugins(REPO / "strategies")
    assert errors == [] and {"example_vector", "example_event"} <= set(plugins)
    store = synthetic_store(tmp_path)
    for plugin_id in ("example_vector", "example_event"):
        config = validate_config({"name": plugin_id, "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-09-03",
                                  "strategy": {"source": "plugin", "id": plugin_id, "params": {}}}, plugins)
        result = run_backtest(config, load_run(store, None, config), TAXES, strategy_factory(config, plugins))
        assert result.trades, plugin_id
        assert "LOOKAHEAD_SUSPECT" not in result.flags
    store.close()


# ----- parameters and timeframes -----


@pytest.mark.parametrize(
    "params, path",
    [({"n": 0}, "strategy.params.n"), ({"n": 2.5}, "strategy.params.n"), ({"m": 1}, "strategy.params.m")],
)
def test_plugin_parameters_are_validated_with_their_paths(tmp_path, params, path):
    write(tmp_path, "plugin", VECTOR)
    plugins, _ = load_plugins(tmp_path)
    with pytest.raises(RunConfigError) as caught:
        validate_config({"name": "x", "market": "US", "symbols": ["AAA"], "start": "2020-01-02",
                         "strategy": {"source": "plugin", "id": "plugin", "params": params}}, plugins)
    assert path in [found for found, _ in caught.value.errors]


def test_an_unknown_plugin_and_a_foreign_timeframe_are_invalid(tmp_path):
    write(tmp_path, "minute", VECTOR.replace('TIMEFRAME = "1d"', 'TIMEFRAME = "5m"'))
    plugins, _ = load_plugins(tmp_path)
    for plugin_id, path in (("missing", "strategy.id"), ("minute", "timeframe")):
        with pytest.raises(RunConfigError) as caught:
            validate_config({"name": "x", "market": "US", "symbols": ["AAA"], "start": "2020-01-02",
                             "strategy": {"source": "plugin", "id": plugin_id}}, plugins)
        assert path in [found for found, _ in caught.value.errors]


# ----- what plugins receive -----


def test_bars_are_read_only_views_with_the_fields_of_section_9_5():
    data = view([[1.0, 2.0], [3.0, 4.0]])
    found = Bars(data)
    assert {name for name in vars(found) if not name.startswith("_")} == {
        "symbols", "times", "session", "open", "high", "low", "close", "volume", "tradable", "rf", "daily"}
    assert_array_equal(found.times, data.days)
    assert_array_equal(found.session, [0, 1])
    for name in ("times", "session", "open", "high", "low", "close", "volume", "tradable", "rf"):
        with pytest.raises(ValueError):
            getattr(found, name)[0] = 0
    assert data.bars.close.flags.writeable  # the run's own arrays stay writable
    with pytest.raises(KeyError):
        found.ext("SPYL1")


def test_ctx_sees_only_rows_up_to_t_and_cannot_write_them(tmp_path):
    seen = []

    class Probe:
        def on_bar(self, book):
            ctx = Context(book, Bars(data), {"n": 2}, {})
            closes = ctx.history("close", 3)
            seen.append(closes[:, 0].tolist())
            with pytest.raises(ValueError):
                closes[0, 0] = 0
            assert ctx.history("close", 100).shape[0] == ctx.t + 1

    data = view([10.0, 11.0, 12.0, 13.0, 14.0])
    simulate(bars([[10.0], [11.0], [12.0], [13.0], [14.0]]), Probe(), settings())
    assert seen == [[10.0], [10.0, 11.0], [10.0, 11.0, 12.0], [11.0, 12.0, 13.0], [12.0, 13.0, 14.0]]


def test_ctx_has_only_the_documented_parts():
    public = {name for name in dir(Context) if not name.startswith("_")}
    assert public == {"t", "time", "symbols", "params", "state", "history", "position", "equity", "cash",
                      "order_target_weight", "order_target_qty", "order_qty", "order_value"}


def test_an_event_plugin_orders_through_ctx_and_keeps_its_state(tmp_path):
    plugin = load_plugin(write(tmp_path, "event", EVENT))
    strategy = PluginStrategy(plugin, {"n": 3})
    result = simulate(bars([[10.0]] * 4), strategy(view([10.0] * 4)), settings(fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[:, 0], [10, 10, 10, 10])


def test_signals_receive_only_bars_and_params(tmp_path):
    source = HEADER + '''
import numpy as np

def signals(*arguments):
    RECEIVED.append(arguments)
    return np.zeros(arguments[0].close.shape)

RECEIVED = []
'''
    plugin = load_plugin(write(tmp_path, "probe", source))
    PluginStrategy(plugin, {"n": 4})(view([1.0, 2.0]))
    ((bars_, params),) = plugin.module.RECEIVED
    assert isinstance(bars_, Bars) and params == {"n": 4}


def test_a_wrong_shape_is_a_plugin_error_and_a_mask_is_passed_through(tmp_path):
    wrong = load_plugin(write(tmp_path, "wrong", HEADER + "\nimport numpy as np\ndef signals(bars, params):\n    return np.zeros(3)\n"))
    with pytest.raises(PluginError, match="shape"):
        PluginStrategy(wrong, {})(view([1.0, 2.0]))
    masked = load_plugin(write(tmp_path, "masked", HEADER + '''
import numpy as np
def signals(bars, params):
    return np.ones(bars.close.shape), np.arange(len(bars.close)) % 2 == 0
'''))
    targets = PluginStrategy(masked, {})(view([1.0, 2.0, 3.0]))
    assert isinstance(targets, Targets) and targets.rebalance.tolist() == [True, False, True]


def test_a_plugin_strategy_pickles_as_its_path_and_parameters(tmp_path):
    plugin = load_plugin(write(tmp_path, "vector", VECTOR))
    copy = pickle.loads(pickle.dumps(PluginStrategy(plugin, {"n": 5})))
    assert copy.plugin.path == plugin.path and copy.params == {"n": 5}
    assert_array_equal(copy(view([1.0, 2.0])).values, PluginStrategy(plugin, {"n": 5})(view([1.0, 2.0])).values)


# ----- the look-ahead check (§9.7) -----


def test_a_plugin_that_reads_one_bar_ahead_is_flagged_and_an_honest_one_is_not(tmp_path):
    rng = np.random.default_rng(4)
    closes = 100 + np.cumsum(rng.normal(size=(60, 2)), axis=0)
    data = view(closes, start=10)
    peeking = load_plugin(write(tmp_path, "peeking", PEEKING))
    # The cuts keep the bars up to 35, 40, 45, 50 and 55 (10 + 50% to 90% of 50). At a cut bar the truncated data have
    # no next close, so the targets differ wherever the next close is higher.
    rises = [(cut, j) for cut in (35, 40, 45, 50, 55) for j in range(2) if closes[cut + 1, j] > closes[cut, j]]
    cut, j = rises[0]
    assert PluginStrategy(peeking, {"n": 3}).lookahead(data) == {"time": int(data.days[cut]), "symbol": "AB"[j]}
    honest = load_plugin(write(tmp_path, "honest", HONEST))
    assert PluginStrategy(honest, {"n": 3}).lookahead(data) is None
    event = load_plugin(write(tmp_path, "event", EVENT))
    assert PluginStrategy(event, {"n": 3}).lookahead(data) is None


def test_the_check_reports_the_earliest_bar_and_symbol(tmp_path):
    def signals(bars, params):
        # Symbol B looks 30 bars ahead from bar 5 on; A is honest.
        out = np.zeros(bars.close.shape)
        later = np.full(len(out), np.nan)
        later[: max(len(out) - 30, 0)] = bars.close[30:, 1]
        out[:, 1] = np.where(np.arange(len(out)) >= 5, np.nan_to_num(later), 0.0)
        return out

    data = view(np.column_stack([np.arange(100.0), 100 + np.arange(100.0) ** 1.1]))
    assert lookahead_check(signals, "WEIGHTS", data, {}) == {"time": int(data.days[21]), "symbol": "B"}


def test_the_flag_travels_with_the_result(tmp_path):
    folder = tmp_path / "strategies"
    folder.mkdir()
    write(folder, "peeking", PEEKING)
    plugins, _ = load_plugins(folder)
    store = synthetic_store(tmp_path / "data")
    config = validate_config({"name": "peek", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-09-03",
                              "strategy": {"source": "plugin", "id": "peeking"}}, plugins)
    result = run_backtest(config, load_run(store, None, config), TAXES, strategy_factory(config, plugins))
    assert set(result.flags["LOOKAHEAD_SUSPECT"]) == {"time", "symbol"}
    store.close()
