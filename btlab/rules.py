"""The rule builder: a rule's JSON, its validation, and its evaluation to DIRECTION targets (SPEC.md §9.6). Pure.

A rule is data: it compares operands of the registry (§8.6) and never executes code (intentional). Validation lists
every error with its code and JSON path; evaluation trusts a valid rule.
"""

import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from btlab.builtins import View
from btlab.engine import Targets
from btlab.features import previous_session
from btlab.indicators import SOURCES
from btlab.params import Param, parse_schema
from btlab.registry import EVERY, Arg, Operand, default_args, operands

VERSION = 1
# §9.6's codes, and INVALID_FIELD for the document's own structure.
ISSUE_CODES = ("UNKNOWN_INDICATOR", "BAD_ARGS", "BAD_OP", "DEPTH", "TF_MISMATCH", "UNKNOWN_PARAM", "UNKNOWN_SERIES",
               "TOO_MANY_CONDITIONS", "INVALID_FIELD")
SIDES = ("long", "short")
OPS = ("gt", "lt", "ge", "le", "cross_above", "cross_below")
MAX_DEPTH = 3
MAX_CONDITIONS = 32
SHIFT = Arg("int", 0, 0, 500)
WINDOW = Arg("int", 1, 1, 500)
REGISTRY = operands()
_RULE_KEYS = ("version", "timeframe", "side", "params", "entry", "exit")
_LOGICAL = ("all", "any", "not")
_CONDITION_KEYS = ("left", "op", "right", "within", "for")
_OPERAND_KEYS = {
    "ind": ("ind", "args", "out", "src", "tf", "shift"),
    "value": ("value",),
    "series": ("series",),
    "feature": ("feature", "args"),
    "today": ("today",),
}
_NOUNS = {"ind": "indicator", "series": "external series", "feature": "feature", "today": "today-so-far value"}
_COMPARE = {"gt": np.greater, "lt": np.less, "ge": np.greater_equal, "le": np.less_equal}


@dataclass(frozen=True)
class RuleIssue:
    """One error of a rule: a code of §9.6, or INVALID_FIELD for the document's structure, with its JSON path."""

    code: str
    path: str
    message: str


def validate_rule(rule: Any, series: Iterable[str] = ()) -> list[RuleIssue]:
    """Every error of a rule; empty when it is valid. series names the external series it may read (§8.5)."""
    return _Check(rule, operands(series)).issues


def schema(rule: dict) -> dict[str, Param]:
    """A valid rule's parameters (§9.2)."""
    return parse_schema(rule.get("params", {}))


def operands_of(rule: dict) -> Iterator[dict]:
    """Every operand of a valid rule."""
    for key in ("entry", "exit"):
        yield from _operands(rule[key])


def _operands(node: dict) -> Iterator[dict]:
    if "not" in node:
        yield from _operands(node["not"])
    elif "all" in node or "any" in node:
        for child in node.get("all", node.get("any")):
            yield from _operands(child)
    else:
        yield node["left"]
        yield node["right"]


def _at(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class _Check:
    """One pass over a rule, collecting its issues."""

    def __init__(self, rule: Any, registry: dict[str, Operand]) -> None:
        self.issues: list[RuleIssue] = []
        self.registry = registry
        self.conditions = 0
        if not isinstance(rule, dict):
            self._add("INVALID_FIELD", "", "a rule must be an object")
            return
        self._unknown(rule, _RULE_KEYS, "", "INVALID_FIELD")
        version = rule.get("version")
        if type(version) is not int or version != VERSION:
            self._add("INVALID_FIELD", "version", f"must be {VERSION}")
        self.timeframe = rule.get("timeframe") if rule.get("timeframe") in EVERY else None
        if self.timeframe is None:
            self._add("INVALID_FIELD", "timeframe", "must be one of " + ", ".join(EVERY))
        if rule.get("side") not in SIDES:
            self._add("INVALID_FIELD", "side", "must be long or short")
        raw = rule.get("params", {})
        self.names = set(raw) if isinstance(raw, dict) else set()
        self.params = self._params(raw)
        for key in ("entry", "exit"):
            if key in rule:
                self._node(rule[key], key, 0)
            else:
                self._add("INVALID_FIELD", key, "is required")
        if self.conditions > MAX_CONDITIONS:
            self._add("TOO_MANY_CONDITIONS", "", f"has {self.conditions} conditions; at most {MAX_CONDITIONS}")

    def _add(self, code: str, path: str, message: str) -> None:
        self.issues.append(RuleIssue(code, path, message))

    def _unknown(self, document: dict, keys: tuple[str, ...], path: str, code: str) -> None:
        for key in document:
            if key not in keys:
                self._add(code, _at(path, str(key)), "is not a known field")

    def _params(self, raw: Any) -> dict[str, Param] | None:
        """The rule's parameters; None when their schema is invalid, and then only their names are known."""
        try:
            return parse_schema(raw)
        except ValueError as invalid:
            self._add("INVALID_FIELD", "params", str(invalid))
            return None

    def _node(self, node: Any, path: str, depth: int) -> None:
        if not isinstance(node, dict):
            self._add("INVALID_FIELD", path, "must be an object")
            return
        logical = [key for key in _LOGICAL if key in node]
        if not logical:
            self._condition(node, path)
            return
        if len(node) != 1:
            self._add("INVALID_FIELD", path, "a logical node holds exactly one of all, any and not")
            return
        if depth == MAX_DEPTH:
            self._add("DEPTH", path, f"at most {MAX_DEPTH} logical levels on any path")
        key = logical[0]
        if key == "not":
            self._node(node[key], f"{path}.not", depth + 1)
        elif not isinstance(node[key], list) or not node[key]:
            self._add("INVALID_FIELD", f"{path}.{key}", "must be a non-empty list of nodes")
        else:
            for index, child in enumerate(node[key]):
                self._node(child, f"{path}.{key}[{index}]", depth + 1)

    def _condition(self, node: dict, path: str) -> None:
        self.conditions += 1
        self._unknown(node, _CONDITION_KEYS, path, "INVALID_FIELD")
        for key in ("left", "right"):
            if key in node:
                self._operand(node[key], f"{path}.{key}")
            else:
                self._add("INVALID_FIELD", f"{path}.{key}", "is required")
        if "op" not in node:
            self._add("INVALID_FIELD", f"{path}.op", "is required")
        elif node["op"] not in OPS:
            self._add("BAD_OP", f"{path}.op", "must be one of " + ", ".join(OPS))
        if "within" in node and "for" in node:
            self._add("BAD_ARGS", path, "within and for exclude each other")
        for key in ("within", "for"):
            if key in node:
                self._number(node[key], f"{path}.{key}", WINDOW)

    def _operand(self, operand: Any, path: str) -> None:
        kinds = [kind for kind in _OPERAND_KEYS if kind in operand] if isinstance(operand, dict) else []
        if len(kinds) != 1:
            self._add("BAD_ARGS", path, "must be exactly one operand: ind, value, series, feature or today")
            return
        kind = kinds[0]
        self._unknown(operand, _OPERAND_KEYS[kind], path, "BAD_ARGS")
        if kind == "value":
            self._number(operand["value"], f"{path}.value", None)
            return
        name = operand[kind]
        found = self.registry.get(name) if isinstance(name, str) else None
        if found is None or found.kind != kind:
            code = "UNKNOWN_SERIES" if kind == "series" else "UNKNOWN_INDICATOR"
            self._add(code, f"{path}.{kind}", f"{name} is not a known {_NOUNS[kind]}")
            return
        if kind == "series":
            return
        self._timeframe(found, operand.get("tf") if kind == "ind" else None, path, kind)
        self._args(found, operand.get("args", {}), f"{path}.args")
        if kind == "ind":
            self._choices(found, operand, path)
            if "shift" in operand:
                self._number(operand["shift"], f"{path}.shift", SHIFT)

    def _timeframe(self, found: Operand, tf: Any, path: str, kind: str) -> None:
        """The operand's bars are the rule's own, or daily ones on a minute timeline (§8.4); minute information enters
        the daily timeline only through features (§9.6)."""
        if self.timeframe is None:
            return
        frame = self.timeframe
        if tf is not None:
            if tf not in EVERY:
                self._add("BAD_ARGS", f"{path}.tf", "must be one of " + ", ".join(EVERY))
                return
            if tf not in (frame, "1d"):
                self._add("TF_MISMATCH", f"{path}.tf", f"must be {frame}" + (" or 1d" if frame != "1d" else ""))
                return
            frame = tf
        if frame not in found.timeframes:
            self._add("TF_MISMATCH", f"{path}.{kind}", f"{found.name} is not available on {frame} bars")

    def _args(self, found: Operand, args: Any, path: str) -> None:
        if not isinstance(args, dict):
            self._add("BAD_ARGS", path, "must be an object")
            return
        for name, value in args.items():
            if name in found.args:
                self._number(value, f"{path}.{name}", found.args[name])
            else:
                self._add("BAD_ARGS", f"{path}.{name}", f"is not an argument of {found.name}")

    def _choices(self, found: Operand, operand: dict, path: str) -> None:
        for key, options in (("src", SOURCES if found.source is not None else ()), ("out", found.outputs)):
            if key in operand and operand[key] not in options:
                message = "must be one of " + ", ".join(options) if options else f"{found.name} takes no {key}"
                self._add("BAD_ARGS", f"{path}.{key}", message)

    def _number(self, value: Any, path: str, field: Arg | None) -> None:
        """A literal within the field's range, or a parameter all of whose values are; field None takes any number."""
        if isinstance(value, dict) and set(value) == {"param"}:
            self._reference(value["param"], path, field)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self._add("BAD_ARGS", path, 'must be a finite number or {"param": name}')
        elif field is not None and field.type == "int" and value != int(value):
            self._add("BAD_ARGS", path, "must be a whole number")
        elif field is not None and not field.min <= value <= field.max:
            self._add("BAD_ARGS", path, f"must be within {field.min:g} to {field.max:g}")

    def _reference(self, name: Any, path: str, field: Arg | None) -> None:
        """A run or an optimization may give a parameter any value from its min to its max (§9.2), so that whole range
        must fit the field."""
        if not isinstance(name, str) or name not in self.names:
            self._add("UNKNOWN_PARAM", path, f"{name} is not a parameter of the rule")
            return
        param = (self.params or {}).get(name)
        if param is None:
            return
        whole = field is not None and field.type == "int"
        if param.type not in ("int", "float") or (whole and param.type != "int"):
            self._add("BAD_ARGS", path, f"must refer to {'an int' if whole else 'a number'} parameter")
        elif field is not None and (param.min is None or param.max is None or param.min < field.min or param.max > field.max):
            self._add("BAD_ARGS", path, f"the parameter {name} needs min and max within {field.min:g} to {field.max:g}")


# ----- evaluation -----


@dataclass(frozen=True, eq=False)
class RuleStrategy:
    """A valid rule with its parameter values as a run's strategy (§9.6). Plain data, so it pickles for a pool worker."""

    rule: dict
    params: dict

    def __call__(self, view: View) -> Targets:
        return Targets("DIRECTION", directions(self.rule, self.params, view))


def directions(rule: dict, params: Mapping[str, Any], view: View,
               series: Mapping[str, np.ndarray] | None = None) -> np.ndarray:
    """A valid rule's DIRECTION targets, bars × symbols: ±1 in position, by its side, and 0 flat. Per symbol, flat until
    entry is true, then in position until exit is true (§9.6), from the warm-up's first bar on like the built-ins.
    series holds the external series it reads, aligned to the bars (§8.5)."""
    evaluation = _Evaluation(rule["timeframe"], params, view, series or {})
    held = _hold(evaluation.node(rule["entry"]), evaluation.node(rule["exit"]))
    return np.where(held, 1.0 if rule["side"] == "long" else -1.0, 0.0)


def _hold(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """One change per bar: in position, exit goes flat; flat, entry enters."""
    held = np.zeros(entry.shape[1], dtype=bool)
    out = np.zeros(entry.shape, dtype=bool)
    for t in range(len(entry)):
        held = np.where(held, ~exit_[t], entry[t])
        out[t] = held
    return out


class _Evaluation:
    """A rule's arrays on one view; each distinct operand is computed once."""

    def __init__(self, timeframe: str, params: Mapping[str, Any], view: View, series: Mapping[str, np.ndarray]) -> None:
        self.timeframe, self.params, self.view, self.series = timeframe, params, view, series
        self.shape = view.bars.close.shape
        self.cache: dict[tuple, np.ndarray] = {}

    def node(self, node: dict) -> np.ndarray:
        if "all" in node:
            return np.logical_and.reduce([self.node(child) for child in node["all"]])
        if "any" in node:
            return np.logical_or.reduce([self.node(child) for child in node["any"]])
        if "not" in node:
            return ~self.node(node["not"])
        hit = _compare(node["op"], self.operand(node["left"]), self.operand(node["right"]))
        if "within" in node:
            return _count(hit, self.value(node["within"])) > 0
        if "for" in node:
            n = self.value(node["for"])
            return _count(hit, n) == n
        return hit

    def value(self, value: Any) -> Any:
        """A field's value, with a parameter substituted."""
        return self.params[value["param"]] if isinstance(value, dict) else value

    def operand(self, operand: dict) -> np.ndarray:
        key = tuple(sorted(
            (name, tuple(sorted((arg, self.value(v)) for arg, v in value.items())) if name == "args" else self.value(value))
            for name, value in operand.items()
        ))
        if key not in self.cache:
            self.cache[key] = self._resolve(operand)
        return self.cache[key]

    def _resolve(self, operand: dict) -> np.ndarray:
        if "value" in operand:
            return np.full(self.shape, float(self.value(operand["value"])))
        if "series" in operand:
            return np.repeat(np.asarray(self.series[operand["series"]], dtype=np.float64)[:, None], self.shape[1], axis=1)
        kind = next(kind for kind in ("ind", "feature", "today") if kind in operand)
        found = REGISTRY[operand[kind]]
        args = default_args(found) | {name: self._arg(found, name, value) for name, value in operand.get("args", {}).items()}
        args |= {key: operand[key] for key in ("src", "out") if key in operand}
        if kind == "feature":
            if self.view.minute_bars is None:
                raise ValueError("a rule that reads features needs the view's minute bars")
            return found.compute(self.view.minute_bars, args)
        if kind == "today":
            return found.compute(self.view.bars, args)
        shift = int(self.value(operand.get("shift", 0)))
        if operand.get("tf", self.timeframe) != self.timeframe:
            # Daily bars on a minute timeline: the last completed session before each bar's own (§8.4).
            daily = self.view.daily
            return previous_session(daily.days, _shifted(found.compute(daily.bars, args), shift), self.view.days)
        return _shifted(found.compute(self.view.bars, args), shift)

    def _arg(self, found: Operand, name: str, value: Any) -> int | float:
        value = self.value(value)
        return int(value) if found.args[name].type == "int" else float(value)


def _compare(op: str, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """NaN compares false, so any NaN makes a condition false (§9.6)."""
    with np.errstate(invalid="ignore"):
        if op in _COMPARE:
            return _COMPARE[op](left, right)
        above, before = (left > right, left <= right) if op == "cross_above" else (left < right, left >= right)
    hit = np.zeros(above.shape, dtype=bool)
    hit[1:] = above[1:] & before[:-1]
    return hit


def _count(hit: np.ndarray, n: int) -> np.ndarray:
    """On how many of the last n bars, t included, the condition held."""
    total = np.cumsum(hit, axis=0)
    window = total.copy()
    window[n:] -= total[:-n]
    return window


def _shifted(values: np.ndarray, k: int) -> np.ndarray:
    """Each bar takes the value k bars earlier."""
    if k == 0:
        return values
    out = np.full(values.shape, np.nan)
    out[k:] = values[:-k]
    return out
