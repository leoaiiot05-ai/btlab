"""The registry of §8.6, and truncation invariance for every operand in it (SPEC.md §8, AC-6)."""

import dataclasses
from datetime import date, datetime, timezone

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab import features
from btlab.indicators import Inputs
from btlab.records import MinuteBars, to_day
from btlab.registry import DAILY, EVERY, MINUTE, Operand, default_args, operands

INDICATORS = ["PRICE", "SMA", "EMA", "RSI", "MACD", "BBANDS", "ATR", "DONCHIAN", "HIGHEST", "LOWEST", "ROC", "ZSCORE",
              "STDEV", "VOLATILITY", "VWAP"]
FEATURES = ["LAST_N_MIN_RETURN", "FIRST_N_MIN_RETURN", "CLOSE_VS_VWAP", "RANGE_POSITION", "RECLAIM_PREV_LOW"]
TODAY = ["TODAY_OPEN", "TODAY_HIGH", "TODAY_LOW", "TODAY_VWAP", "TODAY_RETURN", "MINUTES_SINCE_OPEN"]
REGISTRY = operands()


def test_every_operand_of_section_8_is_registered():
    assert list(REGISTRY) == INDICATORS + FEATURES + TODAY
    assert {name: REGISTRY[name].kind for name in REGISTRY} == {
        **{name: "ind" for name in INDICATORS}, **{name: "feature" for name in FEATURES}, **{name: "today" for name in TODAY}
    }
    assert all(REGISTRY[name].timeframes == EVERY for name in INDICATORS if name != "VWAP")
    assert REGISTRY["VWAP"].timeframes == MINUTE
    assert all(REGISTRY[name].timeframes == DAILY for name in FEATURES)
    assert all(REGISTRY[name].timeframes == MINUTE for name in TODAY)


@pytest.mark.parametrize(
    "name, defaults",
    [
        ("PRICE", {"src": "close"}),
        ("SMA", {"n": 20, "src": "close"}),
        ("EMA", {"n": 20, "src": "close"}),
        ("RSI", {"n": 14}),
        ("MACD", {"fast": 12, "slow": 26, "signal": 9, "out": "macd"}),
        ("BBANDS", {"n": 20, "k": 2.0, "out": "upper"}),
        ("ATR", {"n": 14}),
        ("DONCHIAN", {"n": 20, "out": "upper"}),
        ("HIGHEST", {"n": 20, "src": "high"}),
        ("LOWEST", {"n": 20, "src": "low"}),
        ("ROC", {"n": 20, "src": "close"}),
        ("ZSCORE", {"n": 20, "src": "close"}),
        ("STDEV", {"n": 20, "src": "close"}),
        ("VOLATILITY", {"n": 60}),
        ("VWAP", {}),
        ("LAST_N_MIN_RETURN", {"n": 30}),
        ("FIRST_N_MIN_RETURN", {"n": 30}),
        ("CLOSE_VS_VWAP", {}),
        ("TODAY_RETURN", {}),
    ],
)
def test_defaults_of_section_8_1(name, defaults):
    assert default_args(REGISTRY[name]) == defaults


def test_outputs_and_ranges():
    assert REGISTRY["MACD"].outputs == ("macd", "signal", "hist")
    assert REGISTRY["BBANDS"].outputs == ("upper", "middle", "lower", "pctb", "width")
    assert REGISTRY["DONCHIAN"].outputs == ("upper", "lower", "middle")
    assert (REGISTRY["SMA"].args["n"].min, REGISTRY["SMA"].args["n"].max) == (1, 1000)
    assert REGISTRY["STDEV"].args["n"].min == 2 and REGISTRY["VOLATILITY"].args["n"].min == 2
    assert REGISTRY["BBANDS"].args["k"].type == "float"


def test_external_series_join_by_name_at_run_time():
    found = operands(["SPYL1", "F"])
    assert found["SPYL1"] == Operand("SPYL1", "series") and found["F"].kind == "series"
    assert found["F"].timeframes == EVERY and found["F"].compute is None
    assert "SPYL1" not in operands()
    with pytest.raises(ValueError):
        operands(["SMA"])


# ----- truncation invariance -----

RNG = np.random.default_rng(20261030)
BARS, SESSION_LENGTH = 90, 10


def random_inputs(rows: int, seed: int) -> Inputs:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, size=(rows, 2)), axis=0))
    open_ = close * np.exp(rng.normal(0, 0.005, size=close.shape))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, size=close.shape))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, size=close.shape))
    volume = rng.integers(0, 1000, size=close.shape).astype(float)
    for array in (open_, high, low, close, volume):
        array[:7, 1] = np.nan  # the second symbol starts later
    real = np.isfinite(close) & (rng.uniform(size=close.shape) > 0.1)
    session = np.arange(rows) // SESSION_LENGTH
    return Inputs(open_, high, low, close, volume, 252 * SESSION_LENGTH, session, real,
                  (np.arange(rows) % SESSION_LENGTH + 1).astype(float))


def cut(inputs: Inputs, rows: int) -> Inputs:
    return dataclasses.replace(inputs, **{
        field.name: getattr(inputs, field.name)[:rows]
        for field in dataclasses.fields(inputs) if isinstance(getattr(inputs, field.name), np.ndarray)
    })


def altered(inputs: Inputs, rows: int) -> Inputs:
    """The same bars up to rows, other bars after them."""
    other = random_inputs(BARS, seed=99)
    return dataclasses.replace(inputs, **{
        name: np.concatenate([getattr(inputs, name)[:rows], getattr(other, name)[rows:]])
        for name in ("open", "high", "low", "close", "volume", "real")
    })


ON_BARS = [name for name in INDICATORS + TODAY]


@pytest.mark.parametrize("name", ON_BARS)
@pytest.mark.parametrize("c", [35, 72])
def test_bars_after_c_change_nothing_up_to_c(name, c):
    operand = REGISTRY[name]
    inputs = random_inputs(BARS, seed=1)
    for args in variants(operand):
        full = operand.compute(inputs, args)
        assert np.isfinite(full[c]).any() or name in ("VOLATILITY",) and c < 61, (name, args)
        assert_array_equal(operand.compute(cut(inputs, c + 1), args), full[: c + 1])
        assert_array_equal(operand.compute(altered(inputs, c + 1), args)[: c + 1], full[: c + 1])


def variants(operand: Operand) -> list[dict]:
    """The defaults, plus every output and source."""
    base = default_args(operand)
    found = [base]
    found += [{**base, "out": out} for out in operand.outputs[1:]]
    if operand.source is not None:
        found += [{**base, "src": src} for src in ("open", "high", "low", "volume") if src != operand.source]
    return found


DAYS = [to_day(date(2026, 9, 1)) + i for i in range(30) if (to_day(date(2026, 9, 1)) + i + 3) % 7 < 5]


def random_minutes(seed: int) -> MinuteBars:
    """Bars at random minutes of each US session in DAYS; a few days have none."""
    rng = np.random.default_rng(seed)
    rows = []
    for day in DAYS:
        on = date.fromordinal(date(1970, 1, 1).toordinal() + day)
        start = int(datetime(on.year, on.month, on.day, 13, 30, tzinfo=timezone.utc).timestamp())
        for minute in sorted(rng.choice(390, size=25, replace=False)):
            price = 100 + rng.normal()
            rows.append((start + 60 * int(minute), price, price + rng.uniform(0, 1), price - rng.uniform(0, 1),
                         price + rng.normal(0, 0.3), float(rng.integers(1, 500))))
    t, o, h, l, c, v = (np.array(column) for column in zip(*rows))
    return MinuteBars(t.astype(np.int64), o, h, l, c, v)


@pytest.mark.parametrize("name", FEATURES)
@pytest.mark.parametrize("c", [5, 14])
def test_days_after_c_change_no_feature_up_to_c(name, c):
    operand = REGISTRY[name]
    bars = random_minutes(seed=4)
    covered = np.array([day for day in DAYS if day % 5 != 2])
    multiplier = np.linspace(0.5, 1.0, len(DAYS))
    days = np.array(DAYS)

    def compute(minutes: MinuteBars, count: int) -> np.ndarray:
        s = features.session_bars("US", days[:count], minutes, covered, multiplier[:count])
        return operand.compute([s], default_args(operand))[:, 0]

    full = compute(bars, len(days))
    assert np.isfinite(full[: c + 1]).any()
    end = np.searchsorted(bars.t, int(datetime.combine(date.fromordinal(date(1970, 1, 1).toordinal() + int(days[c]) + 1),
                                                       datetime.min.time(), tzinfo=timezone.utc).timestamp()))
    head = MinuteBars(*(getattr(bars, f.name)[:end] for f in dataclasses.fields(bars)))
    assert_array_equal(compute(head, c + 1), full[: c + 1])
    other = random_minutes(seed=5)
    after = other.t >= bars.t[end] if end < len(bars.t) else np.zeros(len(other.t), dtype=bool)
    mixed = MinuteBars(*(np.concatenate([getattr(head, f.name), getattr(other, f.name)[after]]) for f in dataclasses.fields(bars)))
    assert_array_equal(compute(mixed, len(days))[: c + 1], full[: c + 1])
