"""Parameter schemas shared by built-ins, plugins and rules (SPEC.md §9.2)."""

import math

import pytest

from btlab.params import Param, parse_schema, validate_params

SCHEMA = {
    "n": Param("int", 20, 5, 100, 5),
    "k": Param("float", 2.0, 0.5, 4.0, 0.1),
    "ma": Param("choice", "SMA", choices=("SMA", "EMA")),
    "short": Param("bool", False),
}


def test_defaults_fill_the_missing_parameters():
    assert validate_params(SCHEMA, {"n": 30}, "strategy.params") == ({"n": 30, "k": 2.0, "ma": "SMA", "short": False}, [])
    assert validate_params(SCHEMA, {"n": 30.0, "k": 1}, "p")[0] == {"n": 30, "k": 1.0, "ma": "SMA", "short": False}


@pytest.mark.parametrize(
    "values, path, message",
    [
        ({"n": 2.5}, "p.n", "must be a whole number"),
        ({"n": 4}, "p.n", "must be at least 5"),
        ({"n": 101}, "p.n", "must be at most 100"),
        ({"n": True}, "p.n", "must be a finite number"),
        ({"k": math.inf}, "p.k", "must be a finite number"),
        ({"k": "2"}, "p.k", "must be a finite number"),
        ({"ma": "WMA"}, "p.ma", 'must be one of "SMA", "EMA"'),
        ({"short": 1}, "p.short", "must be true or false"),
        ({"speed": 3}, "p.speed", "is not a parameter"),
    ],
)
def test_each_invalid_value_is_named_by_its_path(values, path, message):
    assert validate_params(SCHEMA, values, "p")[1] == [(path, message)]


def test_values_must_be_an_object():
    assert validate_params(SCHEMA, [20], "p") == ({}, [("p", "must be an object")])


def test_a_schema_parses_from_data():
    parsed = parse_schema({
        "n": {"type": "int", "default": 20, "min": 5, "max": 100, "step": 5, "label_en": "Length", "label_zh": "週期"},
        "mode": {"type": "choice", "default": "fast", "choices": ["fast", "slow"]},
        "flag": {"type": "bool", "default": True},
    })
    assert parsed == {
        "n": Param("int", 20, 5, 100, 5, label_en="Length", label_zh="週期"),
        "mode": Param("choice", "fast", choices=("fast", "slow")),
        "flag": Param("bool", True),
    }
    assert parsed["n"].document() == {"type": "int", "default": 20, "min": 5, "max": 100, "step": 5, "label_en": "Length",
                                      "label_zh": "週期"}
    assert parsed["mode"].document()["choices"] == ["fast", "slow"]


@pytest.mark.parametrize(
    "raw, problem",
    [
        ([1], "a parameter schema must be a mapping"),
        ({"1n": {"type": "int", "default": 1}}, "'1n' is not a parameter name"),
        ({"n": 5}, "n must be a mapping"),
        ({"n": {"type": "decimal", "default": 1}}, "n: type must be one of int, float, bool, choice"),
        ({"n": {"type": "int"}}, "n: default is missing"),
        ({"n": {"type": "int", "default": 1, "min": 5}}, "n: the default must be at least 5"),
        ({"n": {"type": "int", "default": 5, "min": 9, "max": 1}}, "n: min is above max"),
        ({"n": {"type": "int", "default": 5, "step": 0}}, "n: step must be above 0"),
        ({"n": {"type": "int", "default": 5, "colour": "red"}}, "n: unknown key colour"),
        ({"m": {"type": "choice", "default": "a"}}, "m: choices must be a non-empty list of strings"),
        ({"m": {"type": "bool", "default": True, "label_en": 3}}, "m: labels must be strings"),
    ],
)
def test_schema_problems_are_named(raw, problem):
    with pytest.raises(ValueError) as caught:
        parse_schema(raw)
    assert problem in str(caught.value)
