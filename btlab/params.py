"""Parameter schemas shared by built-ins, plugins and rules (SPEC.md §9.2). Pure.

The UI builds its forms from a schema, and optimization ranges default to its min, max and step.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

PARAM_TYPES = ("int", "float", "bool", "choice")
_KEYS = ("type", "default", "min", "max", "step", "choices", "label_en", "label_zh")


@dataclass(frozen=True)
class Param:
    """One parameter: min, max and step for numbers, choices for a choice."""

    type: str
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    label_en: str = ""
    label_zh: str = ""

    def document(self) -> dict[str, Any]:
        """The parameter as JSON for the API, without the fields its type does not use."""
        found = {"type": self.type, "default": self.default, "label_en": self.label_en, "label_zh": self.label_zh}
        if self.type in ("int", "float"):
            found |= {"min": self.min, "max": self.max, "step": self.step}
        if self.type == "choice":
            found["choices"] = list(self.choices)
        return found


def check_value(param: Param, value: Any) -> Any:
    """The value if the parameter takes it; ValueError saying why not."""
    if param.type == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if param.type == "choice":
        if value not in param.choices:
            raise ValueError("must be one of " + ", ".join(f'"{choice}"' for choice in param.choices))
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("must be a finite number")
    if param.type == "int" and value != int(value):
        raise ValueError("must be a whole number")
    if param.min is not None and value < param.min:
        raise ValueError(f"must be at least {param.min:g}")
    if param.max is not None and value > param.max:
        raise ValueError(f"must be at most {param.max:g}")
    return int(value) if param.type == "int" else float(value)


def validate_params(schema: Mapping[str, Param], values: Any, path: str) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Every parameter of the schema, defaults filled in, and the errors (path, message) of the values given."""
    if not isinstance(values, dict):
        return {}, [(path, "must be an object")]
    errors = [(f"{path}.{name}", "is not a parameter") for name in values if name not in schema]
    found = {}
    for name, param in schema.items():
        try:
            found[name] = check_value(param, values[name]) if name in values else param.default
        except ValueError as invalid:
            errors.append((f"{path}.{name}", str(invalid)))
    return found, errors


def parse_schema(raw: Any) -> dict[str, Param]:
    """A schema written as data, such as a plugin's PARAMS or a rule's params. ValueError names every problem."""
    if not isinstance(raw, dict):
        raise ValueError("a parameter schema must be a mapping of names to definitions")
    problems: list[str] = []
    schema = {name: param for name, definition in raw.items() if (param := _definition(name, definition, problems))}
    if problems:
        raise ValueError("; ".join(problems))
    return schema


def _definition(name: Any, definition: Any, problems: list[str]) -> Param | None:
    if not isinstance(name, str) or not name.isidentifier():
        problems.append(f"{name!r} is not a parameter name")
        return None
    if not isinstance(definition, dict):
        problems.append(f"{name} must be a mapping")
        return None
    found = len(problems)
    problems.extend(f"{name}: unknown key {key}" for key in definition if key not in _KEYS)
    kind = definition.get("type")
    if kind not in PARAM_TYPES:
        problems.append(f"{name}: type must be one of {', '.join(PARAM_TYPES)}")
        return None
    numbers = {key: definition.get(key) for key in ("min", "max", "step")}
    if kind in ("int", "float"):
        for key, value in numbers.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                problems.append(f"{name}: {key} must be a finite number")
        if all(isinstance(numbers[key], (int, float)) for key in ("min", "max")) and numbers["min"] > numbers["max"]:
            problems.append(f"{name}: min is above max")
        if isinstance(numbers["step"], (int, float)) and numbers["step"] <= 0:
            problems.append(f"{name}: step must be above 0")
    choices = definition.get("choices", ())
    if kind == "choice" and (not isinstance(choices, (list, tuple)) or not choices or not all(isinstance(c, str) for c in choices)):
        problems.append(f"{name}: choices must be a non-empty list of strings")
    labels = [definition.get(key, "") for key in ("label_en", "label_zh")]
    if not all(isinstance(label, str) for label in labels):
        problems.append(f"{name}: labels must be strings")
    if len(problems) > found:
        return None
    param = Param(kind, definition.get("default"), *(numbers.values() if kind in ("int", "float") else (None,) * 3),
                  tuple(choices) if kind == "choice" else (), *labels)
    if "default" not in definition:
        problems.append(f"{name}: default is missing")
        return None
    try:
        return replace(param, default=check_value(param, param.default))
    except ValueError as invalid:
        problems.append(f"{name}: the default {invalid}")
        return None
