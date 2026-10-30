"""The owner's plugins in both styles, and the look-ahead check (SPEC.md §9.5, §9.7).

Plugin code is the owner's and trusted, but it only ever receives bars, params and ctx: never the store, the config,
IB or a network client. The web API has no route that writes code (§9.5).
"""

import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import numpy as np

from btlab.builtins import View
from btlab.engine import Book, OrderStrategy, Targets
from btlab.indicators import Inputs
from btlab.params import Param, parse_schema

TIMEFRAMES = ("1d", "1m", "5m", "15m", "30m", "60m")
OUTPUTS = {"weights": "WEIGHTS", "direction": "DIRECTION"}
HISTORY_FIELDS = ("open", "high", "low", "close", "volume", "tradable")
# The look-ahead check truncates the data after the bars at these shares of the run (§9.7).
CUT_FRACTIONS = (0.5, 0.6, 0.7, 0.8, 0.9)
TOLERANCE = 1e-9
_PACKAGE = "btlab_plugins"


class PluginError(Exception):
    """A plugin that cannot load, or that breaks its contract while running."""


@dataclass(frozen=True)
class LoadError:
    """A plugin that failed to load, listed by GET /api/strategies (§9.5)."""

    id: str
    path: Path
    error: str


@dataclass(frozen=True, eq=False)
class Plugin:
    """A loaded plugin. style is vector (signals) or event (on_bar); output is WEIGHTS, DIRECTION or ORDERS."""

    id: str
    path: Path
    name_en: str
    name_zh: str
    timeframe: str
    params: dict[str, Param]
    style: str
    output: str
    module: ModuleType


def load_plugins(folder: Path) -> tuple[dict[str, Plugin], list[LoadError]]:
    """Every *.py file of the folder whose name does not start with "_", in name order; a broken one does not stop
    the others."""
    plugins: dict[str, Plugin] = {}
    errors: list[LoadError] = []
    for path in sorted(Path(folder).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            plugins[path.stem] = load_plugin(path)
        except PluginError as error:
            errors.append(LoadError(path.stem, path, str(error)))
    return plugins, errors


def load_plugin(path: Path) -> Plugin:
    """Imports a plugin file and checks the module contract of §9.5; PluginError names every problem."""
    path = Path(path)
    module = _import(path)
    problems = [f"{key} must be a non-empty string" for key in ("NAME_EN", "NAME_ZH") if not _text(getattr(module, key, None))]
    timeframe = getattr(module, "TIMEFRAME", None)
    if timeframe not in TIMEFRAMES:
        problems.append("TIMEFRAME must be one of " + ", ".join(TIMEFRAMES))
    params: dict[str, Param] = {}
    if not hasattr(module, "PARAMS"):
        problems.append("PARAMS is missing")
    else:
        try:
            params = parse_schema(module.PARAMS)
        except ValueError as invalid:
            problems.append(f"PARAMS: {invalid}")
    vector, event = callable(getattr(module, "signals", None)), callable(getattr(module, "on_bar", None))
    if vector == event:
        problems.append("it must define exactly one of signals(bars, params) and on_bar(ctx)")
    output = getattr(module, "OUTPUT", "weights")
    if vector and output not in OUTPUTS:
        problems.append('OUTPUT must be "weights" or "direction"')
    if problems:
        raise PluginError("; ".join(problems))
    return Plugin(path.stem, path, module.NAME_EN, module.NAME_ZH, timeframe, params, "vector" if vector else "event",
                  OUTPUTS[output] if vector else "ORDERS", module)


def _import(path: Path) -> ModuleType:
    name = f"{_PACKAGE}.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass in the plugin looks its module up while it is created
    try:
        spec.loader.exec_module(module)
    except SyntaxError as error:
        sys.modules.pop(name, None)
        raise PluginError(f"syntax error on line {error.lineno}: {error.msg}") from None
    except Exception as error:
        sys.modules.pop(name, None)
        raise PluginError(f"{type(error).__name__} while loading: {error}") from None
    return module


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _readonly(array: Any) -> np.ndarray:
    view = np.asarray(array).view()
    view.flags.writeable = False
    return view


class Bars:
    """What signals(bars, params) receives (§9.5): read-only views of the adjusted bars, bars × symbols, with the
    bars' times (day numbers or epoch seconds), sessions, tradable flags and the risk-free rate per bar. daily is the
    daily bars on a minute timeline, None on the daily one; ext(name) is an external series, aligned (§8.5)."""

    def __init__(self, view: View, times: np.ndarray | None = None, session: np.ndarray | None = None,
                 daily: Any = None, series: Mapping[str, np.ndarray] | None = None) -> None:
        self.symbols = view.symbols
        self.times = _readonly(view.days if times is None else times)
        self.session = _readonly(np.arange(len(view.days)) if session is None else session)
        self.open, self.high, self.low, self.close, self.volume = (
            _readonly(getattr(view.bars, name)) for name in ("open", "high", "low", "close", "volume")
        )
        self.tradable = _readonly(view.tradable)
        self.rf = _readonly(view.rf)
        self.daily = daily
        self._series = dict(series or {})

    def ext(self, name: str) -> np.ndarray:
        if name not in self._series:
            raise KeyError(f"no external series {name}")
        return _readonly(self._series[name])


class Context:
    """What on_bar(ctx) receives at bar t (§9.5): rows up to and including t only, as read-only views, and the orders
    by symbol, which fill according to the fill timing; the last order for a symbol in a bar wins."""

    __slots__ = ("t", "time", "symbols", "params", "state", "_book", "_bars", "_index")

    def __init__(self, book: Book, bars: Bars, params: Mapping[str, Any], state: dict) -> None:
        self.t, self.time, self.symbols, self.params, self.state = book.t, book.time, bars.symbols, params, state
        self._book, self._bars = book, bars
        self._index = {symbol: j for j, symbol in enumerate(bars.symbols)}

    def history(self, field: str, n: int) -> np.ndarray:
        """The last n rows of a field up to and including t (fewer near the start), read-only."""
        if field not in HISTORY_FIELDS:
            raise ValueError(f"history has no field {field}")
        return _readonly(getattr(self._bars, field)[max(0, self.t - n + 1): self.t + 1])

    def position(self, symbol: str) -> float:
        return float(self._book.positions[self._index[symbol]])

    @property
    def equity(self) -> float:
        return self._book.equity

    @property
    def cash(self) -> float:
        return self._book.cash

    def order_target_weight(self, symbol: str, weight: float) -> None:
        self._book.order_target_weight(self._index[symbol], weight)

    def order_target_qty(self, symbol: str, quantity: float) -> None:
        self._book.order_target_qty(self._index[symbol], quantity)

    def order_qty(self, symbol: str, change: float) -> None:
        self._book.order_qty(self._index[symbol], change)

    def order_value(self, symbol: str, amount: float) -> None:
        self._book.order_value(self._index[symbol], amount)


class _Events:
    """An event plugin as the engine's OrderStrategy, with one state dict for the run."""

    def __init__(self, on_bar: Callable[[Context], None], bars: Bars, params: Mapping[str, Any]) -> None:
        self._on_bar, self._bars, self._params, self._state = on_bar, bars, params, {}

    def on_bar(self, book: Book) -> None:
        self._on_bar(Context(book, self._bars, self._params, self._state))


class PluginStrategy:
    """A plugin as a run's strategy: called with the run's View it returns Targets (signals) or an event strategy
    (on_bar). Pickled for a pool worker, it keeps the plugin's path and parameters, and the worker loads the file."""

    def __init__(self, plugin: Plugin, params: Mapping[str, Any]) -> None:
        self.plugin, self.params = plugin, dict(params)

    def __call__(self, view: View) -> "Targets | OrderStrategy":
        bars = Bars(view)
        params = MappingProxyType(dict(self.params))
        if self.plugin.style == "event":
            return _Events(self.plugin.module.on_bar, bars, params)
        return _targets(self.plugin.module.signals, self.plugin.output, bars, dict(self.params))

    def lookahead(self, view: View) -> dict | None:
        """LOOKAHEAD_SUSPECT's detail for a vectorized plugin, or None; event plugins need no check (§9.7)."""
        if self.plugin.style == "event":
            return None
        return lookahead_check(self.plugin.module.signals, self.plugin.output, view, self.params)

    def __getstate__(self) -> dict:
        return {"path": str(self.plugin.path), "params": self.params}

    def __setstate__(self, state: dict) -> None:
        self.plugin, self.params = load_plugin(Path(state["path"])), state["params"]


def _targets(signals: Callable, output: str, bars: Bars, params: dict) -> Targets:
    """signals' targets, with an optional rebalance mask as a second array, checked against the bars' shape."""
    found = signals(bars, params)
    values, mask = found if isinstance(found, tuple) and len(found) == 2 else (found, None)
    values = np.asarray(values, dtype=np.float64)
    if values.shape != bars.close.shape:
        raise PluginError(f"signals returned an array of shape {values.shape} instead of {bars.close.shape}")
    if mask is not None:
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != (len(values),):
            raise PluginError(f"signals returned a rebalance mask of shape {mask.shape} instead of ({len(values)},)")
    return Targets(output, values, mask)


def lookahead_check(signals: Callable, output: str, view: View, params: Mapping[str, Any]) -> dict | None:
    """The targets on the full data and on the data truncated after the bars at 50 to 90% of the run must agree at
    every bar up to the cut (NaN equals NaN; tolerance 1e-9). Returns the earliest differing bar's time and symbol,
    or None. The optimizer calls it for its first and best combinations (§9.7)."""
    full = _targets(signals, output, Bars(view), dict(params))
    run = len(view.days) - view.start
    earliest: tuple[int, str | None] | None = None
    for fraction in CUT_FRACTIONS:
        rows = view.start + int(fraction * run) + 1
        if rows > len(view.days):
            continue
        part = _targets(signals, output, Bars(_cut(view, rows)), dict(params))
        found = _first_difference(full, part, rows, view.symbols)
        if found is not None and (earliest is None or found[0] < earliest[0]):
            earliest = found
    return None if earliest is None else {"time": int(view.days[earliest[0]]), "symbol": earliest[1]}


def _cut(view: View, rows: int) -> View:
    bars = Inputs(*(getattr(view.bars, name)[:rows] for name in ("open", "high", "low", "close", "volume")))
    return replace(view, days=view.days[:rows], bars=bars, tradable=view.tradable[:rows], rf=view.rf[:rows])


def _first_difference(full: Targets, part: Targets, rows: int, symbols: tuple[str, ...]) -> tuple[int, str | None] | None:
    ours, theirs = full.values[:rows], part.values[:rows]
    with np.errstate(invalid="ignore"):
        same = (np.isnan(ours) & np.isnan(theirs)) | (np.abs(ours - theirs) <= TOLERANCE)
    bars = np.flatnonzero(~same.all(axis=1))
    if (full.rebalance is None) != (part.rebalance is None):
        return 0, None
    masks = np.flatnonzero(full.rebalance[:rows] != part.rebalance[:rows]) if full.rebalance is not None else np.zeros(0, int)
    first = min([*bars[:1].tolist(), *masks[:1].tolist()], default=None)
    if first is None:
        return None
    differing = np.flatnonzero(~same[first])
    return first, symbols[differing[0]] if len(differing) else None
