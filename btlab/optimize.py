"""Optimization: the grid, the out-of-sample check and walk-forward analysis (SPEC.md §13).

Every run is an ordinary backtest on data loaded once. The pool is injected: its workers receive the data once,
through the initializer, and run a top-level function on picklable tasks, because Windows spawns them (§13.7).
Results are kept by combination, so the order in which runs finish never matters.
"""

import copy
import functools
import itertools
import math
import multiprocessing
import os
import time
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from btlab.backtest import (
    RunConfig,
    RunConfigError,
    RunData,
    StrategyFactory,
    lookahead,
    run_backtest,
    strategy_factory,
    validate_config,
    window,
)
from btlab.builtins import View
from btlab.engine import Targets
from btlab.metrics import Curve, curve_metrics, trade_metrics
from btlab.plugins import Plugin
from btlab.records import Trade, to_date, to_day
from btlab.taxes import TaxTable

MAX_COMBINATIONS = 2_000
OBJECTIVES = {"SHARPE": "sharpe", "CAGR": "cagr", "SORTINO": "sortino", "CALMAR": "calmar", "PROFIT_FACTOR": "profit_factor"}
MODES = ("GRID", "OOS", "WFO")
# The phases of an optimization's progress, and a walk-forward window's status; the UI localizes each.
PHASES = ("ESTIMATE", *MODES)
WINDOW_STATUSES = ("OK", "NO_RESULT")
MIN_TRADES = 20
CONFIRM_SECONDS = 600.0
IN_SAMPLE_SHARE = 0.7
TRAIN_YEARS, TEST_YEARS, MIN_TEST_MONTHS = 3, 1, 3
_REQUEST_KEYS = ("config", "ranges", "objective", "min_trades", "mode", "confirm_long")

PoolFactory = Callable[..., Executor]


class OptimizeError(Exception):
    """An optimization that cannot run: TOO_MANY_COMBOS (§13.1), or PERIOD_TOO_SHORT for its mode."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# ----- the request (§13.1) -----


@dataclass(frozen=True, eq=False)
class Combination:
    """One set of range values: its run configuration and strategy, or the error that keeps it from running."""

    values: tuple
    config: RunConfig | None
    strategy: StrategyFactory | None
    error: str | None


@dataclass(frozen=True, eq=False)
class Request:
    """A checked request. document is what is stored: the base configuration, complete, with the ranges and the
    settings. combinations are in lexicographic order of the ranges, the last range varying fastest (§13.2)."""

    document: dict
    config: RunConfig
    paths: tuple[str, ...]
    objective: str
    min_trades: int
    mode: str
    confirm_long: bool
    combinations: tuple[Combination, ...]


def parse_request(raw: Any, plugins: Mapping[str, Plugin] | None = None) -> Request:
    """Checks a request and builds its combinations. Invalid fields raise RunConfigError (INVALID_FIELD) with their
    paths, and more than 2,000 combinations OptimizeError TOO_MANY_COMBOS. A combination that is not a valid run
    configuration (fast not below slow, say) is kept with its error and never runs."""
    if not isinstance(raw, dict):
        raise RunConfigError([("", "the request must be an object")])
    errors = [(key, "is not a known field") for key in raw if key not in _REQUEST_KEYS]
    try:
        config = validate_config(raw.get("config"), plugins)
    except RunConfigError as invalid:
        errors.extend((f"config.{path}" if path else "config", message) for path, message in invalid.errors)
        config = None
    ranges = _ranges(raw.get("ranges"), config, errors)
    settings = {
        "objective": _setting(raw, "objective", lambda v: v in OBJECTIVES, "SHARPE", errors,
                              "must be one of " + ", ".join(OBJECTIVES)),
        "min_trades": _setting(raw, "min_trades", lambda v: type(v) is int and v >= 0, MIN_TRADES, errors,
                               "must be a whole number of at least 0"),
        "mode": _setting(raw, "mode", lambda v: v in MODES, "GRID", errors, "must be one of " + ", ".join(MODES)),
    }
    confirm_long = _setting(raw, "confirm_long", lambda v: isinstance(v, bool), False, errors, "must be true or false")
    if errors:
        raise RunConfigError(errors)
    count = math.prod(len(values) for values in ranges.values())
    if count > MAX_COMBINATIONS:
        raise OptimizeError("TOO_MANY_COMBOS", f"{count} combinations; at most {MAX_COMBINATIONS}")
    paths = tuple(ranges)
    combinations = tuple(
        _combination(config.document, paths, values, plugins) for values in itertools.product(*ranges.values())
    )
    if all(combination.config is None for combination in combinations):
        raise RunConfigError([("ranges", f"no combination is a valid configuration: {combinations[0].error}")])
    document = {"config": config.document, "ranges": dict(raw["ranges"]), **settings}
    return Request(document, config, paths, settings["objective"], settings["min_trades"], settings["mode"],
                   confirm_long, combinations)


def combination_config(document: dict, paths: Sequence[str], values: Sequence) -> dict:
    """A combination's run configuration: the base one with the combination's values set. Promoting a combination
    runs it as an ordinary backtest (§13.7)."""
    found = copy.deepcopy(document)
    for path, value in zip(paths, values):
        *parents, key = path.split(".")
        functools.reduce(lambda node, name: node[name], parents, found)[key] = value
    return found


def _setting(raw: dict, key: str, valid: Callable[[Any], bool], default: Any, errors: list[tuple[str, str]],
             message: str) -> Any:
    value = raw.get(key, default)
    if not valid(value):
        errors.append((key, message))
    return value


def _ranges(raw: Any, config: RunConfig | None, errors: list[tuple[str, str]]) -> dict[str, list]:
    """Each path's values, in the order given. A path names a field of the validated base configuration."""
    if not isinstance(raw, dict) or not raw:
        errors.append(("ranges", "must be a non-empty object keyed by JSON path"))
        return {}
    found = {}
    for path, spec in raw.items():
        at = f'ranges["{path}"]'
        if config is not None and not _exists(config.document, path):
            errors.append((at, "is not a field of the configuration"))
            continue
        try:
            found[path] = _values(spec)
        except ValueError as invalid:
            errors.append((at, str(invalid)))
    return found


def _exists(document: dict, path: str) -> bool:
    node: Any = document
    for name in path.split("."):
        if not isinstance(node, dict) or name not in node:
            return False
        node = node[name]
    return True


def _values(spec: Any) -> list:
    """{min, max, step} counts from min by step up to max; {values: [...]} lists them. A range alone above 2,000
    values already makes too many combinations."""
    if isinstance(spec, dict) and set(spec) == {"values"}:
        values = spec["values"]
        if not isinstance(values, list) or not values or not all(_scalar(value) for value in values):
            raise ValueError("values must be a non-empty list of numbers, strings, booleans or null")
        return list(values)
    if not isinstance(spec, dict) or set(spec) != {"min", "max", "step"}:
        raise ValueError("must be {min, max, step} or {values: [...]}")
    low, high, step = spec["min"], spec["max"], spec["step"]
    if not all(_number(value) for value in (low, high, step)) or step <= 0 or high < low:
        raise ValueError("min, max and step must be finite numbers, step above 0 and min at most max")
    count = math.floor((high - low) / step + 1e-9) + 1
    if count > MAX_COMBINATIONS:
        raise OptimizeError("TOO_MANY_COMBOS", f"one range has {count} values; at most {MAX_COMBINATIONS} combinations")
    whole = all(isinstance(value, int) for value in (low, high, step))
    # Rounding keeps 0.1 + 2 × 0.1 at 0.3, so values stay equal across runs and in the heatmap.
    return [low + k * step if whole else round(low + k * step, 10) for k in range(count)]


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, str)) or _number(value)


def _combination(document: dict, paths: tuple[str, ...], values: tuple, plugins: Mapping[str, Plugin] | None) -> Combination:
    try:
        config = validate_config(combination_config(document, paths, values), plugins)
    except RunConfigError as invalid:
        return Combination(values, None, None, str(invalid))
    return Combination(values, config, strategy_factory(config, plugins), None)


# ----- pool workers (§13.2, §13.7) -----


@dataclass(frozen=True, eq=False)
class Task:
    """One run for a worker: the rows of its window (all the data when None), its opening capital, whether it closes
    its positions at its last close, and whether it returns its curve and trades."""

    config: RunConfig
    strategy: StrategyFactory
    rows: tuple[int, int] | None = None
    capital: float | None = None
    flatten: bool = False
    keep: bool = False


@dataclass(frozen=True, eq=False)
class Outcome:
    metrics: dict
    flags: dict
    curve: Curve | None = None
    trades: tuple[Trade, ...] = ()


_WORKER: dict[str, Any] = {}


def start_worker(data: RunData, taxes: TaxTable | None) -> None:
    """The pool initializer: each worker receives the loaded data once (§13.2)."""
    _WORKER.update(data=data, taxes=taxes)


def run_task(task: Task) -> Outcome:
    """A worker's run, on the data its initializer received; grid runs skip the look-ahead check (§9.7)."""
    config, data = task.config, _WORKER["data"]
    if task.rows is not None:
        config, data = window(config, data, *task.rows, capital=task.capital)
    result = run_backtest(config, data, _WORKER["taxes"], task.strategy, check_lookahead=False, flatten=task.flatten)
    if not task.keep:
        return Outcome(result.metrics["strategy"], result.flags)
    return Outcome(result.metrics["strategy"], result.flags, result.curve, result.trades)


class _Cash:
    """No instruction on any bar: a NO_RESULT window's test period stays in cash (§13.5)."""

    def __call__(self, view: View) -> Targets:
        return Targets("WEIGHTS", np.full(view.bars.close.shape, np.nan))


def default_workers() -> int:
    return max(1, (os.cpu_count() or 1) - 1)


def process_pool(workers: int) -> PoolFactory:
    """The real pool (§13.7). Its workers start as Windows starts them, on every platform."""
    return functools.partial(ProcessPoolExecutor, workers, mp_context=multiprocessing.get_context("spawn"))


# ----- running -----


@dataclass(frozen=True)
class NeedsConfirmation:
    """The estimate exceeds 10 minutes and the request did not confirm it: the API answers 409 (§13.6)."""

    estimate_seconds: float


@dataclass(frozen=True)
class Optimization:
    """DONE or CANCELLED (with the results so far), the results by combination and mode, the best combination
    (null for WFO, whose choices are per window) and the flags of every run (§13.7)."""

    status: str
    results: dict
    best: dict | None
    flags: dict


def optimize(
    request: Request, data: RunData, taxes: TaxTable | None, pool: PoolFactory, workers: int, *,
    progress: Callable[[str, int, int], None] = lambda phase, done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.perf_counter,
) -> Optimization | NeedsConfirmation:
    """Estimates the request from one run of its first combination on the full data (§13.6), then runs its mode in
    the pool. progress(phase, done, total) follows each run; cancelled() is asked between runs."""
    windows = _WINDOWS[request.mode](data)
    first = next(index for index, combination in enumerate(request.combinations) if combination.config is not None)
    valid = sum(combination.config is not None for combination in request.combinations)
    progress("ESTIMATE", 0, 1)
    started = clock()
    run_backtest(request.combinations[first].config, data, taxes, request.combinations[first].strategy,
                 check_lookahead=False)
    grids = len(windows) if request.mode == "WFO" else 1
    estimate = (clock() - started) * valid * grids / workers
    if estimate > CONFIRM_SECONDS and not request.confirm_long:
        return NeedsConfirmation(estimate)
    with pool(initializer=start_worker, initargs=(data, taxes)) as executor:
        search = _Search(request, executor, progress, cancelled, valid * grids + len(windows))
        results, best, chosen = _MODES[request.mode](search, data, windows)
    # The look-ahead check runs for the first and the chosen combinations (§9.7).
    checked = (request.combinations[index] for index in dict.fromkeys([first, *chosen]))
    suspects = [lookahead(combination.config, data, combination.strategy) for combination in checked]
    flags = _merged([{"LOOKAHEAD_SUSPECT": found} for found in suspects if found is not None] + search.flags)
    return Optimization("CANCELLED" if search.stopped else "DONE", {"paths": list(request.paths), **results}, best, flags)


class _Search:
    """Runs tasks in the pool, in combination order, counting progress and watching for a cancel."""

    def __init__(self, request: Request, executor: Executor, progress: Callable[[str, int, int], None],
                 cancelled: Callable[[], bool], total: int) -> None:
        self.request, self.executor, self.progress, self.cancelled, self.total = request, executor, progress, cancelled, total
        self.done = 0
        self.stopped = False
        self.flags: list[dict] = []

    def grid(self, phase: str, rows: tuple[int, int] | None) -> list[Outcome | None]:
        """Every valid combination's outcome, None for the others and for those a cancel left undone."""
        futures = [
            None if c.config is None else self.executor.submit(run_task, Task(c.config, c.strategy, rows))
            for c in self.request.combinations
        ]
        return [None if future is None else self._wait(phase, future) for future in futures]

    def run(self, phase: str, task: Task) -> Outcome | None:
        return None if self.stopped else self._wait(phase, self.executor.submit(run_task, task))

    def _wait(self, phase: str, future: Future) -> Outcome | None:
        """The run's outcome; after a cancel, None, and the run is dropped unless it has already started."""
        if self.stopped or self.cancelled():
            self.stopped = True
            future.cancel()
            return None
        outcome = future.result()
        self.done += 1
        self.flags.append(outcome.flags)
        self.progress(phase, self.done, self.total)
        return outcome


def _grid(search: _Search, data: RunData, windows: list) -> tuple[dict, dict | None, list[int]]:
    outcomes = search.grid("GRID", None)
    best = _best(search.request, outcomes)
    return _report(search.request, outcomes, best), _best_document(search.request, outcomes, best), _chosen(best)


def _out_of_sample(search: _Search, data: RunData, windows: list) -> tuple[dict, dict | None, list[int]]:
    """§13.4: the grid on the first 70% of the period's bars, then the best combination on the rest."""
    (train, test_rows), = windows
    outcomes = search.grid("OOS", train)
    best = _best(search.request, outcomes)
    test = None
    if best is not None:
        combination = search.request.combinations[best]
        test = search.run("OOS", Task(combination.config, combination.strategy, test_rows))
    report = _report(search.request, outcomes, best) | {"oos": {
        "split": _iso(data.aligned.days[test_rows[0]]),
        "in_sample": None if best is None else outcomes[best].metrics,
        "out_of_sample": None if test is None else test.metrics,
    }}
    return report, _best_document(search.request, outcomes, best), _chosen(best)


def _walk(search: _Search, data: RunData, windows: list) -> tuple[dict, dict | None, list[int]]:
    """§13.5: each window's grid on its training period, then its best on its test period, starting flat with the
    previous window's final equity and closing at its last close; in cash when no combination qualifies."""
    request, days = search.request, data.aligned.days
    key = OBJECTIVES[request.objective]
    capital = request.config.initial_capital
    reports, curves, trades, chosen = [], [], [], []
    for train, test in windows:
        outcomes = search.grid("WFO", train)
        best = None if search.stopped else _best(request, outcomes)
        combination = None if best is None else request.combinations[best]
        task = (Task(request.config, _Cash(), test, capital, True, True) if combination is None
                else Task(combination.config, combination.strategy, test, capital, True, True))
        outcome = search.run("WFO", task)
        if outcome is None:
            break
        capital = float(outcome.curve.equity[-1])
        curves.append(outcome.curve)
        trades.extend(outcome.trades)
        chosen.extend(_chosen(best))
        reports.append({
            "train": [_iso(days[train[0]]), _iso(days[train[1] - 1])],
            "test": [_iso(days[test[0]]), _iso(days[test[1] - 1])],
            "status": "NO_RESULT" if combination is None else "OK",
            "values": None if combination is None else list(combination.values),
            "objective": None if best is None else outcomes[best].metrics.get(key),
            "metrics": outcome.metrics,
            "objectives": [None if found is None else found.metrics.get(key) for found in outcomes],
            "trades": [None if found is None else found.metrics["trades"] for found in outcomes],
        })
    chained = Curve(*(np.concatenate([getattr(curve, name) for curve in curves]) if curves else np.zeros(0)
                      for name in ("days", "equity", "rf")))
    report = {"windows": reports, "chained": {
        "time_unit": "day", "t": chained.days.astype(int).tolist(), "equity": chained.equity.tolist(),
        "metrics": curve_metrics(chained) | trade_metrics(trades) if curves else None,
    }}
    return report, None, chosen


_MODES = {"GRID": _grid, "OOS": _out_of_sample, "WFO": _walk}


def _best(request: Request, outcomes: list[Outcome | None]) -> int | None:
    """The highest objective among the combinations with at least min_trades closed trades and a defined objective;
    a tie goes to the earlier combination (§13.2)."""
    key = OBJECTIVES[request.objective]
    best, score = None, None
    for index, outcome in enumerate(outcomes):
        if outcome is None or outcome.metrics["trades"] < request.min_trades:
            continue
        value = outcome.metrics.get(key)
        if value is not None and (score is None or value > score):
            best, score = index, value
    return best


def _chosen(best: int | None) -> list[int]:
    return [] if best is None else [best]


def _report(request: Request, outcomes: list[Outcome | None], best: int | None) -> dict:
    return {
        "combinations": [
            {"values": list(c.values), "metrics": None if outcome is None else outcome.metrics, "error": c.error}
            for c, outcome in zip(request.combinations, outcomes)
        ],
        "best": best,
    }


def _best_document(request: Request, outcomes: list[Outcome | None], best: int | None) -> dict | None:
    if best is None:
        return None
    return {"index": best, "values": list(request.combinations[best].values),
            "objective": outcomes[best].metrics[OBJECTIVES[request.objective]]}


def _merged(flag_sets: list[dict]) -> dict:
    """Every flag of the runs, with the detail of its first occurrence."""
    merged: dict = {}
    for flags in flag_sets:
        for name, detail in flags.items():
            merged.setdefault(name, detail)
    return merged


# ----- windows (§13.4, §13.5) -----


def _split(data: RunData) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The rows before and from the split bar: the period's first bar plus floor(0.7 × bars)."""
    count = len(data.aligned.days)
    split = data.start + math.floor(IN_SAMPLE_SHARE * (count - data.start))
    if split - data.start < 2 or count - split < 2:
        raise OptimizeError("PERIOD_TOO_SHORT", "the out-of-sample check needs at least 2 bars on each side")
    return [((data.start, split), (split, count))]


def _walk_forward(data: RunData) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The rows of each window: training on [s, s + 3 years), testing on [s + 3 years, s + 4 years), s moving on by a
    year; a last test window shorter than 3 months is dropped."""
    days = data.aligned.days
    last = int(days[-1])
    found = []
    begin = int(days[data.start])
    while (test := _months(begin, 12 * TRAIN_YEARS)) <= last:
        end = min(_months(begin, 12 * (TRAIN_YEARS + TEST_YEARS)), last + 1)
        if end < _months(test, MIN_TEST_MONTHS):
            break
        first, middle, stop = (int(row) for row in np.searchsorted(days, [begin, test, end]))
        found.append(((first, middle), (middle, stop)))
        begin = _months(begin, 12)
    if not found:
        raise OptimizeError("PERIOD_TOO_SHORT", "walk-forward needs at least 3 years and 3 months")
    return found


_WINDOWS = {"GRID": lambda data: [], "OOS": _split, "WFO": _walk_forward}


def _months(day: int, months: int) -> int:
    """The same day some calendar months later; a day past the month's end becomes its last day (29 February plus a
    year is 28 February)."""
    moment = to_date(day)
    total = moment.month - 1 + months
    year, month = moment.year + total // 12, total % 12 + 1
    return to_day(date(year, month, min(moment.day, monthrange(year, month)[1])))


def _iso(day: int) -> str:
    return to_date(int(day)).isoformat()


# ----- for the API (prompt 16) -----


def heatmap(document: dict, results: dict, x: str, y: str) -> dict:
    """§13.3: the objective over the values of two paths, the other values fixed at the best combination's (the
    first's when there is none, where every cell is null anyway); a cell is null below min_trades or without a run."""
    paths = results["paths"]
    if "combinations" not in results or x == y or x not in paths or y not in paths:
        raise ValueError("x and y must be two paths of a grid or out-of-sample optimization")
    combinations = results["combinations"]
    ix, iy = paths.index(x), paths.index(y)
    entries = {tuple(entry["values"]): entry for entry in combinations}
    fixed = combinations[results["best"] if results["best"] is not None else 0]["values"]
    key = OBJECTIVES[document["objective"]]

    def cell(x_value: Any, y_value: Any) -> float | None:
        values = list(fixed)
        values[ix], values[iy] = x_value, y_value
        entry = entries.get(tuple(values))
        metrics = None if entry is None else entry["metrics"]
        return None if metrics is None or metrics["trades"] < document["min_trades"] else metrics.get(key)

    xs = list(dict.fromkeys(entry["values"][ix] for entry in combinations))
    ys = list(dict.fromkeys(entry["values"][iy] for entry in combinations))
    return {"x": {"path": x, "values": xs}, "y": {"path": y, "values": ys},
            "cells": [[cell(x_value, y_value) for x_value in xs] for y_value in ys]}
