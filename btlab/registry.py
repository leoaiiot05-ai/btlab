"""The registry of operands: every indicator, feature, today-so-far value and external series (SPEC.md §8.6). Pure.

Validation, the rule builder and GET /api/indicators read it. compute(data, args) takes `Inputs` for indicators and
today-so-far values, and one `SessionBars` per symbol for features; args holds every argument, with `src` and `out`
where they apply. External series have no compute here: their values come aligned from the store.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from btlab import features, indicators

DAILY = ("1d",)
MINUTE = ("1m", "5m", "15m", "30m", "60m")
EVERY = DAILY + MINUTE


@dataclass(frozen=True)
class Arg:
    """An int or float argument with its default and inclusive range."""

    type: str
    default: float
    min: float
    max: float


@dataclass(frozen=True)
class Operand:
    """kind is the operand key of a rule (§9.6): ind, feature, today or series. outputs are the `out` choices, the
    default first; source is the default `src`, or None when the operand takes none."""

    name: str
    kind: str
    args: Mapping[str, Arg] = field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    source: str | None = None
    timeframes: tuple[str, ...] = EVERY
    compute: Callable[[Any, dict[str, Any]], np.ndarray] | None = None


def default_args(operand: Operand) -> dict[str, Any]:
    """Every argument at its default, with src and out where the operand takes them."""
    args: dict[str, Any] = {name: arg.default for name, arg in operand.args.items()}
    if operand.source is not None:
        args["src"] = operand.source
    if operand.outputs:
        args["out"] = operand.outputs[0]
    return args


def operands(series: Iterable[str] = ()) -> dict[str, Operand]:
    """The operands of §8.6, plus one external series operand per name given (§8.5): the run-time hook."""
    found = {operand.name: operand for operand in _STATIC}
    for name in series:
        if name in found:
            raise ValueError(f"the series {name} has the name of an indicator")
        found[name] = Operand(name, "series")
    return found


def _length(default: int, minimum: int = 1, maximum: int = 1000) -> Arg:
    return Arg("int", default, minimum, maximum)


def _source(data: indicators.Inputs, args: dict[str, Any]) -> np.ndarray:
    return getattr(data, args["src"])


def _each_symbol(compute: Callable[..., np.ndarray], *names: str) -> Callable[[Any, dict[str, Any]], np.ndarray]:
    """A feature of one symbol's SessionBars, stacked into days × symbols."""
    return lambda sessions, args: np.column_stack([compute(s, *(args[name] for name in names)) for s in sessions])


_STATIC = (
    Operand("PRICE", "ind", source="close", compute=lambda d, a: indicators.price(d, a["src"])),
    Operand("SMA", "ind", {"n": _length(20)}, source="close", compute=lambda d, a: indicators.sma(_source(d, a), a["n"])),
    Operand("EMA", "ind", {"n": _length(20)}, source="close", compute=lambda d, a: indicators.ema(_source(d, a), a["n"])),
    Operand("RSI", "ind", {"n": _length(14)}, compute=lambda d, a: indicators.rsi(d.close, a["n"])),
    Operand(
        "MACD", "ind",
        {"fast": _length(12, 1, 500), "slow": _length(26, 1, 500), "signal": _length(9, 1, 500)},
        outputs=("macd", "signal", "hist"),
        compute=lambda d, a: indicators.macd(d.close, a["fast"], a["slow"], a["signal"])[a["out"]],
    ),
    Operand(
        "BBANDS", "ind", {"n": _length(20, 2), "k": Arg("float", 2.0, 0.1, 10.0)},
        outputs=("upper", "middle", "lower", "pctb", "width"),
        compute=lambda d, a: indicators.bbands(d.close, a["n"], a["k"])[a["out"]],
    ),
    Operand("ATR", "ind", {"n": _length(14)}, compute=lambda d, a: indicators.atr(d.high, d.low, d.close, a["n"])),
    Operand(
        "DONCHIAN", "ind", {"n": _length(20)}, outputs=("upper", "lower", "middle"),
        compute=lambda d, a: indicators.donchian(d.high, d.low, a["n"])[a["out"]],
    ),
    Operand("HIGHEST", "ind", {"n": _length(20)}, source="high", compute=lambda d, a: indicators.highest(_source(d, a), a["n"])),
    Operand("LOWEST", "ind", {"n": _length(20)}, source="low", compute=lambda d, a: indicators.lowest(_source(d, a), a["n"])),
    Operand("ROC", "ind", {"n": _length(20)}, source="close", compute=lambda d, a: indicators.roc(_source(d, a), a["n"])),
    Operand("ZSCORE", "ind", {"n": _length(20, 2)}, source="close", compute=lambda d, a: indicators.zscore(_source(d, a), a["n"])),
    Operand("STDEV", "ind", {"n": _length(20, 2)}, source="close", compute=lambda d, a: indicators.stdev(_source(d, a), a["n"])),
    Operand(
        "VOLATILITY", "ind", {"n": _length(60, 2)},
        compute=lambda d, a: indicators.volatility(d.close, a["n"], d.periods_per_year),
    ),
    Operand(
        "VWAP", "ind", timeframes=MINUTE, compute=lambda d, a: indicators.vwap(d.high, d.low, d.close, d.volume, d.session)
    ),
    Operand("LAST_N_MIN_RETURN", "feature", {"n": _length(30, 1, 390)}, timeframes=DAILY,
            compute=_each_symbol(features.last_n_min_return, "n")),
    Operand("FIRST_N_MIN_RETURN", "feature", {"n": _length(30, 1, 390)}, timeframes=DAILY,
            compute=_each_symbol(features.first_n_min_return, "n")),
    Operand("CLOSE_VS_VWAP", "feature", timeframes=DAILY, compute=_each_symbol(features.close_vs_vwap)),
    Operand("RANGE_POSITION", "feature", timeframes=DAILY, compute=_each_symbol(features.range_position)),
    Operand("RECLAIM_PREV_LOW", "feature", timeframes=DAILY, compute=_each_symbol(features.reclaim_prev_low)),
    Operand("TODAY_OPEN", "today", timeframes=MINUTE, compute=lambda d, a: features.today_open(d)),
    Operand("TODAY_HIGH", "today", timeframes=MINUTE, compute=lambda d, a: features.today_high(d)),
    Operand("TODAY_LOW", "today", timeframes=MINUTE, compute=lambda d, a: features.today_low(d)),
    Operand("TODAY_VWAP", "today", timeframes=MINUTE, compute=lambda d, a: features.today_vwap(d)),
    Operand("TODAY_RETURN", "today", timeframes=MINUTE, compute=lambda d, a: features.today_return(d)),
    Operand("MINUTES_SINCE_OPEN", "today", timeframes=MINUTE, compute=lambda d, a: features.minutes_since_open(d)),
)
