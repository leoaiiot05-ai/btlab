"""Optimization: requests, the grid, the out-of-sample check, walk-forward, the pool and the estimate (SPEC.md §13,
AC-16, AC-29)."""

from datetime import date

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.backtest import RunConfigError, RunData, load_run, run_backtest, validate_config, window
from btlab.optimize import (
    NeedsConfirmation,
    Optimization,
    OptimizeError,
    Outcome,
    _best,
    combination_config,
    heatmap,
    optimize,
    parse_request,
    process_pool,
)
from btlab.plugins import load_plugin
from btlab.prices import align
from btlab.records import DailyBars, to_date, to_day
from builders import TAXES, synthetic_store, walk
from fakes import InlinePool
from test_plugins import HONEST, PEEKING, write

BASE = {"name": "grid", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-06-03",
        "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 10, "slow": 30}}}
FAST_SLOW = {"strategy.params.fast": {"values": [5, 10]}, "strategy.params.slow": {"min": 20, "max": 40, "step": 10}}


def request(ranges: dict, **settings) -> dict:
    return {"config": BASE, "ranges": ranges, **settings}


def iso(day: int) -> str:
    return to_date(int(day)).isoformat()


@pytest.fixture
def store(tmp_path):
    opened = synthetic_store(tmp_path)
    yield opened
    opened.close()


def run(parsed, data, workers=1, **options):
    return optimize(parsed, data, TAXES, InlinePool, workers, **options)


# ----- the request (§13.1) -----


def test_combinations_are_enumerated_lexicographically():
    parsed = parse_request(request(FAST_SLOW))
    assert parsed.paths == ("strategy.params.fast", "strategy.params.slow")
    assert [c.values for c in parsed.combinations] == [(5, 20), (5, 30), (5, 40), (10, 20), (10, 30), (10, 40)]
    assert parsed.combinations[4].config.strategy["params"] == {"fast": 10, "slow": 30, "ma": "SMA", "allow_short": False}
    assert (parsed.objective, parsed.min_trades, parsed.mode, parsed.confirm_long) == ("SHARPE", 20, "GRID", False)
    assert parsed.document == {"config": parsed.config.document, "ranges": FAST_SLOW, "objective": "SHARPE",
                               "min_trades": 20, "mode": "GRID"}


@pytest.mark.parametrize(
    "spec, values",
    [({"min": 0.1, "max": 0.3, "step": 0.1}, [0.1, 0.2, 0.3]), ({"min": 1, "max": 10, "step": 4}, [1, 5, 9]),
     ({"values": [None, 2.5]}, [None, 2.5])],
)
def test_range_values(spec, values):
    parsed = parse_request(request({"exits.stop_loss_pct": spec}))
    assert [c.values[0] for c in parsed.combinations] == values
    assert [c.config.exits.stop_loss_pct for c in parsed.combinations] == values


def test_at_most_2000_combinations():
    assert len(parse_request(request({"initial_capital": {"min": 1_000, "max": 2_999, "step": 1}})).combinations) == 2_000
    for ranges in ({"initial_capital": {"min": 1_000, "max": 3_000, "step": 1}},
                   {"initial_capital": {"min": 1_000, "max": 1_044, "step": 1}, "max_position_pct": {"min": 1, "max": 45, "step": 1}}):
        with pytest.raises(OptimizeError) as caught:
            parse_request(request(ranges))
        assert caught.value.code == "TOO_MANY_COMBOS"


@pytest.mark.parametrize(
    "raw, path",
    [
        (request({"strategy.params.speed": {"values": [1]}}), 'ranges["strategy.params.speed"]'),
        (request({"exits.ibguard.tp_pct": {"values": [10]}}), 'ranges["exits.ibguard.tp_pct"]'),
        (request({"strategy.params.fast": {"min": 5, "max": 10, "step": 0}}), 'ranges["strategy.params.fast"]'),
        (request({"strategy.params.fast": {"values": []}}), 'ranges["strategy.params.fast"]'),
        (request({"strategy.params.fast": {"values": [[5]]}}), 'ranges["strategy.params.fast"]'),
        (request({"strategy.params.fast": [5, 10]}), 'ranges["strategy.params.fast"]'),
        (request({}), "ranges"),
        (request({"strategy.params.fast": {"values": [60]}}), "ranges"),
        (request(FAST_SLOW, objective="MAX"), "objective"),
        (request(FAST_SLOW, min_trades=-1), "min_trades"),
        (request(FAST_SLOW, mode="RANDOM"), "mode"),
        (request(FAST_SLOW, confirm_long="yes"), "confirm_long"),
        ({**request(FAST_SLOW), "seed": 1}, "seed"),
        ({**request(FAST_SLOW), "config": {**BASE, "start": "soon"}}, "config.start"),
        ({"ranges": FAST_SLOW}, "config"),
    ],
)
def test_request_errors_name_their_paths(raw, path):
    with pytest.raises(RunConfigError) as caught:
        parse_request(raw)
    assert path in [found for found, _ in caught.value.errors], caught.value.errors


def test_a_promoted_combination_is_its_run_configuration():
    parsed = parse_request(request(FAST_SLOW))
    promoted = combination_config(parsed.document["config"], list(parsed.document["ranges"]), [10, 30])
    assert validate_config(promoted).document == parsed.combinations[4].config.document


# ----- the grid and its best (§13.2, §13.3) -----


def expected_best(entries: list[dict], key: str, min_trades: int) -> int | None:
    found = [(e["metrics"][key], -index) for index, e in enumerate(entries)
             if e["metrics"] and e["metrics"]["trades"] >= min_trades and e["metrics"][key] is not None]
    return -max(found)[1] if found else None


def test_the_best_needs_min_trades_and_ties_go_to_the_earlier_combination(store):
    ranges = {"name": {"values": ["a", "b"]}, "strategy.params.fast": {"values": [5, 10, 20]}}
    data = load_run(store, None, parse_request(request(ranges)).config)
    first = run(parse_request(request(ranges, min_trades=0)), data)
    entries = first.results["combinations"]
    assert [e["metrics"] for e in entries[:3]] == [e["metrics"] for e in entries[3:]]  # the name changes nothing
    trades = sorted({e["metrics"]["trades"] for e in entries})
    for min_trades in (0, trades[1], trades[-1] + 1):
        found = run(parse_request(request(ranges, min_trades=min_trades)), data)
        index = expected_best(entries, "sharpe", min_trades)
        assert (found.best or {}).get("index") == index
        assert index is None or (index < 3 and found.best["values"] == entries[index]["values"])


def test_an_undefined_objective_never_qualifies():
    parsed = parse_request(request(FAST_SLOW, objective="PROFIT_FACTOR", min_trades=1))
    outcomes = [Outcome({"trades": 3, "profit_factor": None}, {}), Outcome({"trades": 3, "profit_factor": 0.5}, {}), None]
    assert _best(parsed, outcomes) == 1


def test_invalid_combinations_are_reported_and_never_run(store):
    parsed = parse_request(request({"strategy.params.fast": {"values": [10, 60]}, "strategy.params.slow": {"values": [50]}}))
    assert parsed.combinations[1].config is None and "fast" in parsed.combinations[1].error
    pools = []

    def pool(**options):
        pools.append(InlinePool(**options))
        return pools[-1]

    result = optimize(parsed, load_run(store, None, parsed.config), TAXES, pool, 1)
    first, second = result.results["combinations"]
    assert first["metrics"]["trades"] >= 0 and first["error"] is None
    assert second["metrics"] is None and "fast" in second["error"]
    assert pools[0].ran == 1


def test_the_heatmap_fixes_the_other_values_at_the_best():
    def entry(values, trades=20, metrics=True):
        a, b, c = values
        return {"values": values, "error": None,
                "metrics": {"sharpe": float(100 * a + 10 * b + c), "trades": trades} if metrics else None}

    combinations = [entry([a, b, c], 5 if (a, b, c) == (2, 1, 1) else 20, (a, b, c) != (1, 2, 1))
                    for a in (1, 2) for b in (1, 2, 3) for c in (1, 2)]
    results = {"paths": ["a", "b", "c"], "combinations": combinations, "best": 4}  # values (1, 3, 1)
    found = heatmap({"objective": "SHARPE", "min_trades": 10}, results, "a", "b")
    assert found["x"] == {"path": "a", "values": [1, 2]} and found["y"] == {"path": "b", "values": [1, 2, 3]}
    assert found["cells"] == [[111.0, None], [None, 221.0], [131.0, 231.0]]
    with pytest.raises(ValueError):
        heatmap({"objective": "SHARPE", "min_trades": 10}, results, "a", "a")


# ----- windows (§13.4, §13.5) -----


def test_a_window_run_equals_a_backtest_of_its_period(store):
    parsed = parse_request(request(FAST_SLOW))
    data = load_run(store, None, parsed.config)
    days = data.aligned.days
    first, stop = data.start + 60, data.start + 200
    combination = parsed.combinations[0]
    windowed = run_backtest(*window(combination.config, data, first, stop), TAXES, combination.strategy)
    alone = validate_config({**combination.config.document, "start": iso(days[first]), "end": iso(days[stop - 1])})
    direct = run_backtest(alone, load_run(store, None, alone), TAXES, combination.strategy)
    assert windowed.metrics == direct.metrics and windowed.trades == direct.trades
    assert_array_equal(windowed.series["EQUITY"], direct.series["EQUITY"])
    assert windowed.series["TIME"][0] == days[first] and windowed.series["EQUITY"][0] == combination.config.initial_capital


def test_the_out_of_sample_check_splits_at_70_percent(store):
    parsed = parse_request(request(FAST_SLOW, mode="OOS", min_trades=0))
    data = load_run(store, None, parsed.config)
    result = run(parsed, data)
    days = data.aligned.days
    split = data.start + int(0.7 * (len(days) - data.start))
    best = parsed.combinations[result.best["index"]]
    inside = run_backtest(*window(best.config, data, data.start, split), TAXES, best.strategy)
    outside = run_backtest(*window(best.config, data, split, len(days)), TAXES, best.strategy)
    assert result.results["oos"] == {"split": iso(days[split]), "in_sample": inside.metrics["strategy"],
                                     "out_of_sample": outside.metrics["strategy"]}
    assert result.results["combinations"][result.best["index"]]["metrics"] == inside.metrics["strategy"]


def daily(days: np.ndarray, close: np.ndarray) -> DailyBars:
    return DailyBars(days, close * 0.999, close * 1.01, close * 0.99, close, np.full(len(days), 1e6),
                     np.zeros(len(days), dtype=bool))


def memory_data(first: date, last: date) -> RunData:
    """AAA, BBB and SPY on the weekdays from first to last, without warm-up."""
    days = np.array([day for day in range(to_day(first), to_day(last) + 1) if (day + 3) % 7 < 5], dtype=np.int64)
    aligned = align("US", days, {symbol: (daily(days, walk(seed, len(days))), []) for seed, symbol in enumerate(("AAA", "BBB"))})[0]
    benchmark = align("US", days, {"SPY": (daily(days, walk(9, len(days))), [])})[0]
    return RunData(aligned, 0, benchmark, "ETF", np.zeros(len(days)), ("STOCK", "STOCK"), (None, None), ((), ()), {})


def walk_forward(**settings) -> dict:
    return {"config": {**BASE, "start": "2015-01-05"}, "mode": "WFO",
            "ranges": {"strategy.params.fast": {"values": [5, 10]}}, **settings}


@pytest.mark.parametrize(
    "last, tests",
    [
        # 2020-01-05 to 2020-03-20 is shorter than 3 months: the last window is dropped.
        (date(2020, 3, 20), [["2018-01-05", "2019-01-04"], ["2019-01-07", "2020-01-03"]]),
        (date(2020, 4, 30), [["2018-01-05", "2019-01-04"], ["2019-01-07", "2020-01-03"], ["2020-01-06", "2020-04-30"]]),
    ],
)
def test_walk_forward_windows_train_three_years_and_test_one(last, tests):
    result = run(parse_request(walk_forward(min_trades=0)), memory_data(date(2015, 1, 5), last))
    windows = result.results["windows"]
    assert [w["test"] for w in windows] == tests
    assert [w["train"] for w in windows] == [["2015-01-05", "2018-01-04"], ["2016-01-05", "2019-01-04"],
                                             ["2017-01-05", "2020-01-03"]][:len(tests)]
    assert all(w["status"] == "OK" and len(w["objectives"]) == len(w["trades"]) == 2 for w in windows)


def test_the_chained_curve_carries_equity_from_window_to_window():
    parsed = parse_request(walk_forward(min_trades=0))
    result = run(parsed, memory_data(date(2015, 1, 5), date(2020, 3, 20)))
    windows, chained = result.results["windows"], result.results["chained"]
    second = chained["t"].index(to_day(date(2019, 1, 7)))
    assert chained["equity"][0] == parsed.config.initial_capital
    assert chained["equity"][second] == chained["equity"][second - 1]  # flat on its first bar, with the last equity
    assert all(w["metrics"]["open_trades"] == 0 for w in windows)  # closed at each window's last close
    assert chained["metrics"]["trades"] == sum(w["metrics"]["trades"] for w in windows) > 0
    assert result.best is None and all(w["values"] in ([5], [10]) for w in windows)


def test_a_window_without_a_qualifying_combination_stays_in_cash():
    parsed = parse_request(walk_forward(min_trades=10_000))
    result = run(parsed, memory_data(date(2015, 1, 5), date(2020, 3, 20)))
    windows = result.results["windows"]
    assert [(w["status"], w["values"], w["metrics"]["trades"]) for w in windows] == [("NO_RESULT", None, 0)] * 2
    assert set(result.results["chained"]["equity"]) == {parsed.config.initial_capital}


def test_a_period_too_short_for_its_mode():
    with pytest.raises(OptimizeError) as caught:
        run(parse_request(walk_forward()), memory_data(date(2015, 1, 5), date(2018, 3, 1)))
    assert caught.value.code == "PERIOD_TOO_SHORT"


# ----- estimate, progress, cancel, flags and the pool (§13.6, §13.7, §9.7) -----


def test_the_estimate_asks_for_confirmation_above_ten_minutes(store):
    parsed = parse_request(request(FAST_SLOW))
    data = load_run(store, None, parsed.config)

    def clock():
        return iter([0.0, 101.0]).__next__

    assert run(parsed, data, clock=clock()) == NeedsConfirmation(606.0)  # 101 s × 6 combinations / 1 worker
    assert isinstance(run(parsed, data, workers=2, clock=clock()), Optimization)
    assert isinstance(run(parse_request({**request(FAST_SLOW), "confirm_long": True}), data, clock=clock()), Optimization)
    wfo = parse_request(walk_forward())
    assert run(wfo, memory_data(date(2015, 1, 5), date(2020, 3, 20)), clock=iter([0.0, 151.0]).__next__) == \
        NeedsConfirmation(604.0)  # 151 s × 2 combinations × 2 windows


def test_a_cancel_keeps_the_partial_results(store):
    parsed = parse_request(request(FAST_SLOW))
    seen = []
    result = run(parsed, load_run(store, None, parsed.config), progress=lambda *step: seen.append(step),
                 cancelled=lambda: len(seen) >= 3)
    assert seen == [("ESTIMATE", 0, 1), ("GRID", 1, 6), ("GRID", 2, 6)]
    assert result.status == "CANCELLED"
    assert [e["metrics"] is not None for e in result.results["combinations"]] == [True, True, False, False, False, False]


def test_the_look_ahead_check_flags_a_peeking_plugin(store, tmp_path):
    plugins = {name: load_plugin(write(tmp_path, name, source)) for name, source in (("peeking", PEEKING), ("honest", HONEST))}
    for name, flagged in (("peeking", True), ("honest", False)):
        raw = {"config": {**BASE, "strategy": {"source": "plugin", "id": name}}, "ranges": {"strategy.params.n": {"values": [3, 5]}}}
        parsed = parse_request(raw, plugins)
        result = run(parsed, load_run(store, None, parsed.config))
        assert ("LOOKAHEAD_SUSPECT" in result.flags) == flagged
        assert "SURVIVORSHIP" in result.flags


def test_a_real_pool_gives_the_in_process_results(store):
    parsed = parse_request(request({"strategy.params.fast": {"values": [5, 10]}}, mode="OOS", min_trades=0))
    data = load_run(store, None, parsed.config)
    inline = run(parsed, data)
    assert optimize(parsed, data, TAXES, process_pool(2), 2) == inline
    assert run(parsed, data) == inline  # deterministic (AC-29)
