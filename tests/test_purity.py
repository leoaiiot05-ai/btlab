"""The modules SPEC.md §2 marks pure do no I/O, read no clock and log nothing (CLAUDE.md)."""

import ast
from pathlib import Path

import pytest

BTLAB = Path(__file__).resolve().parents[1] / "btlab"
# Pure modules as they arrive; later prompts add theirs. records.py holds the shared records (plan of prompt 2).
PURE = ["markets", "records", "calendar", "prices"]
PURE_IMPORTS = {
    "collections", "collections.abc", "dataclasses", "datetime", "enum", "functools", "itertools", "math", "re",
    "typing", "zoneinfo", "numpy",
}
FORBIDDEN_CALLS = ("open(", "datetime.now", "date.today", "time.time", "time.monotonic", "print(")


def imports_of(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", PURE)
def test_pure_modules_import_only_pure_code(module):
    for name in imports_of(BTLAB / f"{module}.py"):
        if name.startswith("btlab."):
            assert name.removeprefix("btlab.") in PURE, name
        else:
            assert name in PURE_IMPORTS, name


@pytest.mark.parametrize("module", PURE)
def test_pure_modules_read_no_clock_and_open_no_file(module):
    source = (BTLAB / f"{module}.py").read_text(encoding="utf-8")
    for call in FORBIDDEN_CALLS:
        assert call not in source, call
