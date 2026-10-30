"""The rule builder: validation, evaluation, backtests and truncation invariance (SPEC.md §9.6, AC-14)."""

import math
import pickle
from dataclasses import replace

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.backtest import RunConfigError, RunData, load_run, minute_view, run_backtest, strategy_factory, validate_config
from btlab.builtins import BUILTINS, Daily, View
from btlab.calendar import session
from btlab.features import session_bars
from btlab.indicators import Inputs
from btlab.intraday import minute_timeline
from btlab.prices import align
from btlab.records import DailyBars, ExternalSeries, MinuteBars, MinuteCoverage, to_date
from btlab.rules import RuleStrategy, directions, operands_of, validate_rule
from btlab.store import MinuteStore
from builders import NOW, TAXES, WEEKDAYS, synthetic_store, walk

NAN = math.nan
SYMBOLS = ("AAA", "BBB")
CLOSE = {"ind": "PRICE"}
N_PARAM = {"type": "int", "default": 5, "min": 2, "max": 50, "step": 1}


def cond(left: dict, op: str, right: dict, **extra) -> dict:
    return {"left": left, "op": op, "right": right, **extra}


def ind(name: str, **fields) -> dict:
    return {"ind": name, **fields}


def value(number) -> dict:
    return {"value": number}


def rule(entry: dict, exit_: dict | None = None, **changes) -> dict:
    """A daily long rule; by default it exits when its entry is false, so its direction is the entry itself."""
    return {"version": 1, "timeframe": "1d", "side": "long", "entry": entry,
            "exit": {"not": entry} if exit_ is None else exit_, **changes}


def issues(document, series=()) -> list[tuple[str, str]]:
    return [(issue.code, issue.path) for issue in validate_rule(document, series)]


def daily_view(closes, minute_bars=None, days=None) -> View:
    close = np.array(closes, dtype=np.float64)
    close = close[:, None] if close.ndim == 1 else close
    days = WEEKDAYS[:len(close)] if days is None else days
    bars = Inputs(close * 0.999, close * 1.01, close * 0.99, close, np.full(close.shape, 1e6))
    return View(SYMBOLS[:close.shape[1]], days, bars, np.isfinite(close), np.zeros(len(close)), 0, minute_bars=minute_bars)


def one_minute(day: int, closes, offsets=None) -> MinuteBars:
    """1-minute bars at minute offsets from the session's open, consecutive from 0 unless given."""
    close = np.array(closes, dtype=np.float64)
    offsets = np.arange(len(close)) if offsets is None else np.array(offsets)
    return MinuteBars(session("US", int(day))[0] + 60 * offsets.astype(np.int64), close - 0.1, close + 0.2, close - 0.2,
                      close, np.full(len(close), 100.0))


def joined(parts: list[MinuteBars]) -> MinuteBars:
    return MinuteBars(*(np.concatenate([getattr(p, name) for p in parts]) for name in ("t", "open", "high", "low", "close", "volume")))


def aligned_of(days: np.ndarray, closes: np.ndarray):
    """Daily bars of the symbols, days × symbols closes, without actions."""
    histories = {
        symbol: (DailyBars(days, c, c + 1, c - 1, c, np.full(len(days), 1e6), np.zeros(len(days), dtype=bool)), [])
        for symbol, c in zip(SYMBOLS, np.asarray(closes, dtype=np.float64).T)
    }
    return align("US", days, histories)[0]


def minute_view_of(aligned, minutes: list[MinuteBars], covered: list[np.ndarray], timeframe: str = "5m") -> View:
    run = minute_timeline("US", timeframe, aligned, minutes, covered, int(aligned.days[0]))
    count = len(minutes)
    data = RunData(aligned, 0, aligned, "ETF", np.zeros(len(aligned.days)), ("STOCK",) * count, (None,) * count,
                   ((),) * count, {})
    return minute_view(run, data)


# ----- validation -----

EXAMPLE = {
    "version": 1,
    "timeframe": "1d",
    "side": "long",
    "params": {"n": {"type": "int", "default": 20, "min": 5, "max": 100, "step": 5}},
    "entry": {"all": [
        {"left": {"ind": "SMA", "args": {"n": {"param": "n"}}}, "op": "cross_above",
         "right": {"ind": "SMA", "args": {"n": 50}}},
        {"left": {"feature": "CLOSE_VS_VWAP"}, "op": "gt", "right": {"value": 0}},
    ]},
    "exit": {"any": [
        {"left": {"ind": "RSI", "args": {"n": 14}}, "op": "gt", "right": {"value": 70}, "for": 2},
    ]},
}


def base(**changes) -> dict:
    """A valid daily rule: SMA(n) above SMA(20) to enter, at or below it to exit."""
    fast, slow = ind("SMA", args={"n": {"param": "n"}}), ind("SMA", args={"n": 20})
    return {"version": 1, "timeframe": "1d", "side": "long", "params": {"n": N_PARAM},
            "entry": {"all": [cond(fast, "gt", slow)]}, "exit": cond(fast, "le", slow), **changes}


def entry_with(timeframe: str = "1d", **fields) -> dict:
    found = base(timeframe=timeframe)
    found["entry"]["all"][0].update(fields)
    return found


def entry_without(key: str) -> dict:
    found = base()
    del found["entry"]["all"][0][key]
    return found


def test_the_example_of_section_9_6_is_valid():
    assert validate_rule(EXAMPLE) == []
    assert validate_rule(base()) == []


@pytest.mark.parametrize(
    "document, expected",
    [
        pytest.param(entry_with(left=ind("NOPE")), [("UNKNOWN_INDICATOR", "entry.all[0].left.ind")], id="unknown"),
        pytest.param(entry_with(left={"today": "SMA"}), [("UNKNOWN_INDICATOR", "entry.all[0].left.today")], id="wrong kind"),
        pytest.param(entry_with(left=ind("SMA", args={"n": 0})), [("BAD_ARGS", "entry.all[0].left.args.n")], id="range"),
        pytest.param(entry_with(left=ind("SMA", args={"n": 2.5})), [("BAD_ARGS", "entry.all[0].left.args.n")], id="whole"),
        pytest.param(entry_with(left=ind("SMA", args={"m": 3})), [("BAD_ARGS", "entry.all[0].left.args.m")], id="arg"),
        pytest.param(entry_with(left=ind("SMA", args=[3])), [("BAD_ARGS", "entry.all[0].left.args")], id="args"),
        pytest.param(entry_with(left=ind("RSI", src="high")), [("BAD_ARGS", "entry.all[0].left.src")], id="no src"),
        pytest.param(entry_with(left=ind("SMA", src="vwap")), [("BAD_ARGS", "entry.all[0].left.src")], id="src"),
        pytest.param(entry_with(left=ind("BBANDS", out="top")), [("BAD_ARGS", "entry.all[0].left.out")], id="out"),
        pytest.param(entry_with(left=ind("SMA", shift=501)), [("BAD_ARGS", "entry.all[0].left.shift")], id="shift"),
        pytest.param(entry_with(left=ind("SMA", tf="2h")), [("BAD_ARGS", "entry.all[0].left.tf")], id="tf"),
        pytest.param(entry_with(left=ind("SMA", when=1)), [("BAD_ARGS", "entry.all[0].left.when")], id="key"),
        pytest.param(entry_with(left={"ind": "SMA", "value": 1}), [("BAD_ARGS", "entry.all[0].left")], id="two kinds"),
        pytest.param(entry_with(left=[1]), [("BAD_ARGS", "entry.all[0].left")], id="no operand"),
        pytest.param(entry_with(right=value(True)), [("BAD_ARGS", "entry.all[0].right.value")], id="value"),
        pytest.param(entry_with(right=value(math.inf)), [("BAD_ARGS", "entry.all[0].right.value")], id="infinite"),
        pytest.param(entry_with(within=0), [("BAD_ARGS", "entry.all[0].within")], id="within"),
        pytest.param(entry_with(**{"for": 501}), [("BAD_ARGS", "entry.all[0].for")], id="for"),
        pytest.param(entry_with(within=2, **{"for": 2}), [("BAD_ARGS", "entry.all[0]")], id="within and for"),
        pytest.param(entry_with(op="above"), [("BAD_OP", "entry.all[0].op")], id="op"),
        pytest.param(entry_with(left={"today": "TODAY_OPEN"}), [("TF_MISMATCH", "entry.all[0].left.today")], id="today"),
        pytest.param(entry_with(left=ind("VWAP")), [("TF_MISMATCH", "entry.all[0].left.ind")], id="vwap"),
        pytest.param(entry_with(left=ind("SMA", tf="5m")), [("TF_MISMATCH", "entry.all[0].left.tf")], id="minute tf"),
        pytest.param(entry_with("5m", left={"feature": "RANGE_POSITION"}), [("TF_MISMATCH", "entry.all[0].left.feature")],
                     id="feature"),
        pytest.param(entry_with("5m", left=ind("SMA", tf="15m")), [("TF_MISMATCH", "entry.all[0].left.tf")], id="other tf"),
        pytest.param(entry_with("5m", left=ind("VWAP", tf="1d")), [("TF_MISMATCH", "entry.all[0].left.ind")], id="daily vwap"),
        pytest.param(entry_with(left=ind("SMA", args={"n": {"param": "m"}})), [("UNKNOWN_PARAM", "entry.all[0].left.args.n")],
                     id="param"),
        pytest.param(entry_with(right=value({"param": "m"})), [("UNKNOWN_PARAM", "entry.all[0].right.value")], id="value param"),
        pytest.param(entry_with(right={"series": "SPYL1"}), [("UNKNOWN_SERIES", "entry.all[0].right.series")], id="series"),
        pytest.param(base(version=2), [("INVALID_FIELD", "version")], id="version"),
        pytest.param(base(version=True), [("INVALID_FIELD", "version")], id="boolean version"),
        pytest.param(base(timeframe="2h"), [("INVALID_FIELD", "timeframe")], id="timeframe"),
        pytest.param(base(side="both"), [("INVALID_FIELD", "side")], id="side"),
        pytest.param(base(comment="mine"), [("INVALID_FIELD", "comment")], id="unknown key"),
        pytest.param(base(params={"n": {"type": "int"}}), [("INVALID_FIELD", "params")], id="params"),
        pytest.param(base(entry={"all": []}), [("INVALID_FIELD", "entry.all")], id="empty all"),
        pytest.param(base(entry={"not": cond(CLOSE, "gt", value(1)), "any": []}), [("INVALID_FIELD", "entry")], id="two keys"),
        pytest.param(base(entry={"any": [5]}), [("INVALID_FIELD", "entry.any[0]")], id="node"),
        pytest.param(entry_with(when=1), [("INVALID_FIELD", "entry.all[0].when")], id="condition key"),
        pytest.param(entry_without("op"), [("INVALID_FIELD", "entry.all[0].op")], id="no op"),
        pytest.param(entry_without("right"), [("INVALID_FIELD", "entry.all[0].right")], id="no right"),
        pytest.param({key: v for key, v in base().items() if key != "exit"}, [("INVALID_FIELD", "exit")], id="no exit"),
    ],
)
def test_each_error_has_its_code_and_path(document, expected):
    assert issues(document) == expected


def test_a_parameter_must_fit_every_field_it_feeds():
    floating = base(params={"n": N_PARAM, "level": {"type": "float", "default": 0.5, "min": 0, "max": 1}})
    floating["entry"]["all"][0]["right"] = ind("SMA", args={"n": {"param": "level"}})
    assert issues(floating) == [("BAD_ARGS", "entry.all[0].right.args.n")]
    wide = base(params={"n": {**N_PARAM, "min": 0}})
    unbounded = base(params={"n": {"type": "int", "default": 5}})
    for document in (wide, unbounded):
        assert issues(document) == [("BAD_ARGS", "entry.all[0].left.args.n"), ("BAD_ARGS", "exit.left.args.n")]
    choice = base(params={"n": N_PARAM, "kind": {"type": "choice", "default": "a", "choices": ["a"]}})
    choice["exit"]["right"] = value({"param": "kind"})
    assert issues(choice) == [("BAD_ARGS", "exit.right.value")]


def test_parameters_may_feed_every_numeric_field_and_series_are_named_by_the_caller():
    document = entry_with(within={"param": "n"}, right=value({"param": "n"}))
    document["entry"]["all"][0]["left"] = ind("ROC", args={"n": {"param": "n"}}, shift={"param": "n"})
    assert validate_rule(document) == []
    assert validate_rule(entry_with(right={"series": "SPYL1"}), series=["SPYL1"]) == []


def test_every_error_is_returned_at_once():
    document = base(version=2)
    document["entry"]["all"][0]["op"] = "above"
    document["exit"]["left"] = ind("NOPE")
    assert issues(document) == [("INVALID_FIELD", "version"), ("BAD_OP", "entry.all[0].op"), ("UNKNOWN_INDICATOR", "exit.left.ind")]
    assert issues("SMA > 20") == [("INVALID_FIELD", "")]


def nested(levels: int) -> dict:
    """levels logical nodes around one condition, alternating all and not from the inside."""
    node = cond(CLOSE, "gt", value(1))
    for k in range(levels):
        node = {"not": node} if k % 2 else {"all": [node]}
    return node


def test_three_logical_levels_pass_and_four_fail():
    flat = cond(CLOSE, "lt", value(1))
    assert validate_rule(rule(nested(3), flat)) == []
    assert issues(rule(nested(4), flat)) == [("DEPTH", "entry.not.all[0].not")]
    assert issues(rule(flat, nested(5))) == [("DEPTH", "exit.all[0].not.all[0]")]


def test_32_conditions_pass_and_33_fail():
    def many(count: int) -> dict:
        return {"any": [cond(CLOSE, "gt", value(k)) for k in range(count)]}

    assert validate_rule(rule(many(31), cond(CLOSE, "lt", value(1)))) == []
    assert issues(rule(many(32), cond(CLOSE, "lt", value(1)))) == [("TOO_MANY_CONDITIONS", "")]


def test_operands_of_lists_every_operand():
    assert [next(iter(operand)) for operand in operands_of(EXAMPLE)] == ["ind", "ind", "feature", "value", "ind", "value"]


# ----- evaluation -----


@pytest.mark.parametrize(
    "op, expected",
    [("gt", [0, 0, 1, 0, 0]), ("ge", [0, 1, 1, 1, 1]), ("lt", [1, 0, 0, 0, 0]), ("le", [1, 1, 0, 1, 1])],
)
def test_each_comparison_at_its_boundary(op, expected):
    view = daily_view([1.0, 2.0, 3.0, 2.0, 2.0])
    assert directions(rule(cond(CLOSE, op, value(2.0))), {}, view)[:, 0].tolist() == expected


@pytest.mark.parametrize(
    "op, closes, expected",
    [
        ("cross_above", [1, 2, 3, 3, 2, 2, 3], [0, 0, 1, 0, 0, 0, 1]),
        ("cross_below", [3, 2, 1, 2, 1, 1, 3], [0, 0, 1, 0, 1, 0, 0]),
    ],
)
def test_crosses_need_the_previous_bar_at_or_beyond_the_line(op, closes, expected):
    assert directions(rule(cond(CLOSE, op, value(2.0))), {}, daily_view(closes))[:, 0].tolist() == expected


@pytest.mark.parametrize(
    "extra, expected",
    [({}, [1, 0, 1, 1, 1, 0, 0, 0]), ({"within": 2}, [1, 1, 1, 1, 1, 1, 0, 0]), ({"for": 2}, [0, 0, 0, 1, 1, 0, 0, 0]),
     ({"for": 3}, [0, 0, 0, 0, 1, 0, 0, 0])],
)
def test_within_and_for(extra, expected):
    view = daily_view([3, 1, 3, 3, 3, 1, 1, 1])
    assert directions(rule(cond(CLOSE, "gt", value(2), **extra)), {}, view)[:, 0].tolist() == expected


@pytest.mark.parametrize(
    "op, expected",
    [("gt", [1, 0, 1, 0, 0, 1]), ("le", [0, 0, 0, 1, 0, 0]), ("cross_above", [0, 0, 0, 0, 0, 0]),
     ("cross_below", [0, 0, 0, 1, 0, 0])],
)
def test_nan_makes_a_condition_false(op, expected):
    view = daily_view([3.0, NAN, 3.0, 1.0, NAN, 3.0])
    assert directions(rule(cond(CLOSE, op, value(2.0))), {}, view)[:, 0].tolist() == expected


def test_shift_reads_k_bars_earlier():
    view = daily_view([1.0, 3.0, 2.0, 4.0])
    assert directions(rule(cond(CLOSE, "gt", ind("PRICE", shift=1))), {}, view)[:, 0].tolist() == [0, 1, 0, 1]
    assert directions(rule(cond(CLOSE, "gt", ind("PRICE", shift=2))), {}, view)[:, 0].tolist() == [0, 0, 1, 1]


def test_parameters_are_substituted():
    params = {"n": N_PARAM, "k": {"type": "int", "default": 1, "min": 1, "max": 5},
              "level": {"type": "float", "default": 0.0, "min": -1.0, "max": 1.0}}
    with_params = rule(cond(ind("ROC", args={"n": {"param": "n"}}, shift={"param": "k"}), "gt", value({"param": "level"}),
                            within={"param": "k"}), params=params)
    literal = rule(cond(ind("ROC", args={"n": 3}, shift=2), "gt", value(0.01), within=2))
    view = daily_view(np.column_stack([walk(1, 80), walk(2, 80)]))
    found = directions(with_params, {"n": 3, "k": 2, "level": 0.01}, view)
    assert_array_equal(found, directions(literal, {}, view))
    assert 0 < found.mean() < 1


def test_a_rule_holds_its_position_from_entry_to_exit():
    view = daily_view([1, 3, 2, 1, 2, 3, 0])
    entry, exit_ = cond(CLOSE, "ge", value(3)), cond(CLOSE, "le", value(1))
    assert directions(rule(entry, exit_), {}, view)[:, 0].tolist() == [0, 1, 1, 0, 0, 1, 0]
    assert directions(rule(entry, exit_, side="short"), {}, view)[:, 0].tolist() == [0, -1, -1, 0, 0, -1, 0]


def test_a_bar_changes_the_state_once():
    # Flat with both true enters; in position with both true exits.
    view = daily_view([1, 3, 3, 2])
    found = directions(rule(cond(CLOSE, "ge", value(2)), cond(CLOSE, "ge", value(3))), {}, view)
    assert found[:, 0].tolist() == [0, 1, 0, 1]


LIKE_A = rule(cond(ind("SMA", args={"n": 10}), "gt", ind("SMA", args={"n": 30})),
              cond(ind("SMA", args={"n": 10}), "le", ind("SMA", args={"n": 30})))


def test_a_rule_like_built_in_a_gives_its_directions():
    view = daily_view(np.column_stack([walk(3, 300), walk(4, 300)]))
    found = directions(LIKE_A, {}, view)
    expected = BUILTINS["MA_CROSS"].run(view, {"fast": 10, "slow": 30, "ma": "SMA", "allow_short": False}).values
    known = np.isfinite(expected)
    assert_array_equal(found[known], expected[known])
    assert (~known).any() and (found[~known] == 0).all()  # flat where A gives no instruction yet
    assert {0.0, 1.0} <= set(found.ravel().tolist())


def test_a_series_reads_its_aligned_values_for_every_symbol():
    days = WEEKDAYS[:4]
    values = np.full(int(days[-1] - days[0]) + 1, 9.0)
    values[days - days[0]] = [-1.0, 1.0, NAN, 2.0]
    view = replace(daily_view(np.ones((4, 2))), external={"SPYL1": ExternalSeries("SPYL1", int(days[0]), values, False)})
    found = directions(rule(cond({"series": "SPYL1"}, "gt", value(0))), {}, view)
    assert found.tolist() == [[0, 0], [1, 1], [0, 0], [1, 1]] and view.read == {"SPYL1"}


def test_features_need_the_views_minute_bars():
    with pytest.raises(ValueError):
        directions(rule(cond({"feature": "RANGE_POSITION"}, "gt", value(0.5))), {}, daily_view([1.0, 2.0]))


def test_a_daily_rule_reads_features_and_a_day_without_coverage_is_false():
    days = WEEKDAYS[:4]
    minutes = joined([one_minute(days[0], [100, 101, 102]), one_minute(days[1], [102, 101, 100]),
                      one_minute(days[3], [100, 100.5, 102])])
    by_day = session_bars("US", days, minutes, days[[0, 1, 3]], np.ones(4))
    view = daily_view([102.0, 100.0, 50.0, 102.0], (by_day,), days)
    found = directions(rule(cond({"feature": "RANGE_POSITION"}, "gt", value(0.5))), {}, view)
    assert found[:, 0].tolist() == [1, 0, 0, 1]  # positions 0.92, 0.08, none, 0.92


# ----- minute timelines (§8.3, §8.4) -----

MINUTE_DAYS = WEEKDAYS[:4]


def stepped_minute_view() -> View:
    """One symbol: daily closes 10, 20, 30 and 40; in each session four 5-minute bars closing at 15, 25, 35 and 45."""
    aligned = aligned_of(MINUTE_DAYS, np.array([[10.0], [20.0], [30.0], [40.0]]))
    minutes = joined([one_minute(day, np.repeat([15.0, 25.0, 35.0, 45.0], 5)) for day in MINUTE_DAYS])
    return minute_view_of(aligned, [minutes], [MINUTE_DAYS])


@pytest.mark.parametrize(
    "operand, expected",
    [
        # Session 0 has no completed session before it.
        (ind("PRICE", tf="1d"), [0, 0, 0, 0, 1, 1, 1, 1, 0, 1, 1, 1, 0, 0, 1, 1]),
        (ind("PRICE", tf="1d", shift=1), [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 1, 1, 1]),
        (ind("SMA", args={"n": 2}, tf="1d"), [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 1, 1]),
    ],
)
def test_a_daily_operand_on_a_minute_timeline_reads_the_previous_session(operand, expected):
    view = stepped_minute_view()
    document = {**rule(cond(CLOSE, "gt", operand)), "timeframe": "5m"}
    assert validate_rule(document) == []
    assert directions(document, {}, view)[:, 0].tolist() == expected


def test_today_operands_on_a_minute_timeline():
    view = stepped_minute_view()
    since_open = {**rule(cond({"today": "MINUTES_SINCE_OPEN"}, "ge", value(10))), "timeframe": "5m"}
    assert directions(since_open, {}, view)[:, 0].tolist() == [0, 1, 1, 1] * 4
    above_open = {**rule(cond(CLOSE, "gt", {"today": "TODAY_OPEN"})), "timeframe": "5m"}
    assert directions(above_open, {}, view)[:, 0].tolist() == [1, 1, 1, 1] * 4  # each session opens at 14.9


# ----- truncation invariance -----

DAILY_RULE = {
    "version": 1, "timeframe": "1d", "side": "short", "params": {"n": N_PARAM},
    "entry": {"all": [
        cond(ind("SMA", args={"n": {"param": "n"}}), "lt", ind("EMA", args={"n": 20})),
        {"not": cond(ind("RSI"), "lt", value(30), within=3)},
        {"any": [cond({"feature": "RANGE_POSITION"}, "lt", value(0.6)),
                 cond(ind("ZSCORE", args={"n": 10}), "gt", value(1), **{"for": 2})]},
    ]},
    "exit": {"any": [
        cond(ind("BBANDS", out="lower"), "gt", CLOSE),
        cond(ind("DONCHIAN", args={"n": 10}, out="upper"), "lt", ind("PRICE", src="high", shift=1)),
        cond({"feature": "LAST_N_MIN_RETURN", "args": {"n": 30}}, "gt", value(0.001)),
    ]},
}
TRUNCATION_DAYS = WEEKDAYS[:150]
DAILY_CLOSES = np.column_stack([walk(5, 150), walk(6, 150)])
OFFSETS = [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 360, 389]
DAY_MINUTES = [
    joined([one_minute(day, walk(100 * j + k, len(OFFSETS)), OFFSETS) for k, day in enumerate(TRUNCATION_DAYS)])
    for j in range(2)
]
DAY_COVERAGE = [np.delete(TRUNCATION_DAYS, np.arange(0, 150, 7)), np.delete(TRUNCATION_DAYS, np.arange(3, 150, 5))]


def featured_view(count: int) -> View:
    """The daily view of the first count days, with only the minute bars and coverage known by then."""
    days = TRUNCATION_DAYS[:count]
    end = session("US", int(days[-1]))[1]
    minute_bars = tuple(
        session_bars("US", days, MinuteBars(*(getattr(bars, name)[bars.t < end] for name in ("t", "open", "high", "low", "close", "volume"))),
                     covered[covered <= days[-1]], np.ones(count))
        for bars, covered in zip(DAY_MINUTES, DAY_COVERAGE)
    )
    return daily_view(DAILY_CLOSES[:count], minute_bars, days)


def test_daily_rules_are_truncation_invariant():
    assert validate_rule(DAILY_RULE) == []
    full = directions(DAILY_RULE, {"n": 5}, featured_view(150))
    assert {-1.0, 0.0} <= set(full.ravel().tolist())
    for count in (40, 77, 100, 149):
        assert_array_equal(directions(DAILY_RULE, {"n": 5}, featured_view(count)), full[:count])


MINUTE_RULE = {
    "version": 1, "timeframe": "5m", "side": "long",
    "entry": {"all": [
        cond(CLOSE, "gt", ind("SMA", args={"n": 2}, tf="1d")),
        cond({"today": "MINUTES_SINCE_OPEN"}, "ge", value(10)),
        {"any": [cond(CLOSE, "gt", ind("VWAP")), cond(ind("RSI", args={"n": 3}), "lt", value(60), within=3)]},
    ]},
    "exit": {"any": [
        cond(CLOSE, "lt", {"today": "TODAY_OPEN"}),
        cond(ind("PRICE", tf="1d", shift=1), "gt", CLOSE, **{"for": 2}),
    ]},
}
SESSION_DAYS = WEEKDAYS[4:8]
SESSION_CLOSES = np.column_stack([walk(7, 8), walk(8, 8)])
SESSION_MINUTES = [
    joined([one_minute(day, SESSION_CLOSES[4 + k, j] * walk(10 * j + k, 60) / 100) for k, day in enumerate(SESSION_DAYS)])
    for j in range(2)
]
SESSION_COVERAGE = [SESSION_DAYS, SESSION_DAYS[[0, 1, 3]]]


def session_view(last: int | None = None) -> View:
    """The minute view, or the one known at the end of bar last: minute bars up to its end, daily bars of the sessions
    before its own."""
    if last is None:
        return minute_view_of(aligned_of(WEEKDAYS[:8], SESSION_CLOSES), SESSION_MINUTES, SESSION_COVERAGE)
    full = session_view()
    end, day = full.times[last] + 300, full.days[last]
    known = int(np.searchsorted(WEEKDAYS[:8], day, side="right"))
    minutes = [MinuteBars(*(getattr(bars, name)[bars.t < end] for name in ("t", "open", "high", "low", "close", "volume")))
               for bars in SESSION_MINUTES]
    view = minute_view_of(aligned_of(WEEKDAYS[:known], SESSION_CLOSES[:known]), minutes,
                          [covered[covered <= day] for covered in SESSION_COVERAGE])
    before = known - 1
    bars = Inputs(*(getattr(view.daily.bars, name)[:before] for name in ("open", "high", "low", "close", "volume")))
    return replace(view, daily=Daily(view.daily.days[:before], bars))


def test_minute_rules_are_truncation_invariant():
    assert validate_rule(MINUTE_RULE) == []
    full = directions(MINUTE_RULE, {}, session_view())
    assert {0.0, 1.0} <= set(full.ravel().tolist())
    for last in (5, 14, 30, 41, len(full) - 2):
        assert_array_equal(directions(MINUTE_RULE, {}, session_view(last)), full[:last + 1])


# ----- backtests -----


def config(**changes) -> dict:
    return {"name": "rule", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-06-03",
            "strategy": {"source": "rule", "rule": LIKE_A}, **changes}


PARAMETRIC = rule(cond(ind("SMA", args={"n": {"param": "n"}}), "gt", ind("SMA", args={"n": 30})),
                  cond(ind("SMA", args={"n": {"param": "n"}}), "le", ind("SMA", args={"n": 30})), params={"n": N_PARAM})


def test_a_rule_run_carries_the_complete_rule():
    validated = validate_config(config(strategy={"source": "rule", "id": "trend", "rule": PARAMETRIC}))
    assert validated.document["strategy"] == {"source": "rule", "id": "trend", "rule": PARAMETRIC, "params": {"n": 5}}
    unnamed = validate_config(config(strategy={"source": "rule", "rule": PARAMETRIC, "params": {"n": 8}}))
    assert unnamed.document["strategy"] == {"source": "rule", "id": None, "rule": PARAMETRIC, "params": {"n": 8}}


@pytest.mark.parametrize(
    "strategy, path, start",
    [
        ({"source": "rule", "rule": entry_with(op="above")}, "strategy.rule.entry.all[0].op", "BAD_OP:"),
        ({"source": "rule", "rule": base(version=2)}, "strategy.rule.version", "INVALID_FIELD:"),
        ({"source": "rule", "rule": [LIKE_A]}, "strategy.rule", "must be an object"),
        ({"source": "rule"}, "strategy.rule", "is required"),
        ({"source": "rule", "rule": PARAMETRIC, "params": {"n": 1}}, "strategy.params.n", "must be at least 2"),
        ({"source": "rule", "rule": LIKE_A, "params": {"x": 1}}, "strategy.params.x", "is not a parameter"),
        ({"source": "rule", "rule": {**LIKE_A, "timeframe": "5m"}}, "timeframe", "must be 5m"),
        ({"source": "rule", "rule": {**LIKE_A, "side": "short"}}, "allow_short", "must be true"),
        ({"source": "builtin", "id": "MA_CROSS", "rule": LIKE_A}, "strategy.rule", "is not a known field"),
    ],
)
def test_rule_configurations_name_each_error(strategy, path, start):
    with pytest.raises(RunConfigError) as caught:
        validate_config(config(strategy=strategy))
    assert any(found == path and message.startswith(start) for found, message in caught.value.errors), caught.value.errors


def test_a_short_rule_runs_when_shorts_are_allowed():
    validated = validate_config(config(strategy={"source": "rule", "rule": {**LIKE_A, "side": "short"}}, allow_short=True))
    assert validated.strategy["rule"]["side"] == "short"


@pytest.fixture
def store(tmp_path):
    opened = synthetic_store(tmp_path)
    yield opened
    opened.close()


def test_a_rule_like_built_in_a_backtests_identically(store):
    results = []
    for strategy in ({"source": "rule", "rule": LIKE_A},
                     {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 10, "slow": 30}}):
        validated = validate_config(config(strategy=strategy))
        results.append(run_backtest(validated, load_run(store, None, validated), TAXES, strategy_factory(validated)))
    by_rule, by_builtin = results
    assert by_rule.trades == by_builtin.trades and len(by_rule.trades) > 2
    assert_array_equal(by_rule.series["EQUITY"], by_builtin.series["EQUITY"])


def test_a_rule_with_features_loads_the_minute_bars(store, tmp_path):
    minute = MinuteStore(tmp_path)
    days = [int(day) for day in store.daily_bars("US", "SPY").day[-6:]]
    for day in days:
        for symbol in SYMBOLS:
            minute.save_minute_day("US", symbol, MinuteCoverage(day, "OK", 1.0, NOW), one_minute(day, 100 + 0.01 * np.arange(390)))
    featured = rule(cond({"feature": "RANGE_POSITION"}, "gt", value(0.5)), cond(CLOSE, "lt", value(0)))
    validated = validate_config(config(strategy={"source": "rule", "rule": featured}))
    data = load_run(store, minute, validated)
    assert data.minutes is not None
    trades = run_backtest(validated, data, TAXES, strategy_factory(validated)).trades
    # Each symbol enters after the first covered day's close, at the next open, and holds.
    assert [(t.symbol, to_date(t.entry_time), t.exit_reason) for t in trades] == [
        (symbol, to_date(days[1]), "OPEN") for symbol in SYMBOLS
    ]
    assert run_backtest(validated, load_run(store, None, validated), TAXES, strategy_factory(validated)).trades == ()
    plain = validate_config(config())
    assert load_run(store, minute, plain).minutes is None
    minute.close()


def test_a_rule_strategy_pickles_for_a_pool_worker():
    strategy = RuleStrategy(PARAMETRIC, {"n": 5})
    view = daily_view(np.column_stack([walk(3, 120), walk(4, 120)]))
    restored = pickle.loads(pickle.dumps(strategy))
    assert_array_equal(restored(view).values, strategy(view).values)
    assert restored(view).kind == "DIRECTION"
