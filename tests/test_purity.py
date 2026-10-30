"""The modules SPEC.md §2 marks pure do no I/O, read no clock and log nothing (CLAUDE.md)."""

import ast
from pathlib import Path

import pytest

BTLAB = Path(__file__).resolve().parents[1] / "btlab"
# Pure modules as they arrive; later prompts add theirs. records.py holds the shared records (plan of prompt 2).
PURE = ["markets", "records", "calendar", "prices", "indicators", "features", "registry", "costs", "sizing", "engine"]
# taxes.py reads its file once at startup; its TaxTable is pure, so pure modules may use it (SPEC.md §2).
PURE_AFTER_LOADING = ["taxes"]
PURE_IMPORTS = {
    "collections", "collections.abc", "dataclasses", "datetime", "enum", "functools", "itertools", "math", "re",
    "typing", "zoneinfo", "numpy",
}
FORBIDDEN_CALLS = {"open", "print", "datetime.now", "datetime.utcnow", "date.today", "time.time", "time.monotonic"}


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
            assert name.removeprefix("btlab.") in PURE + PURE_AFTER_LOADING, name
        elif name == "btlab":
            continue  # "from btlab import x" names its modules; they are checked as x below
        else:
            assert name in PURE_IMPORTS or name.startswith("numpy."), name


@pytest.mark.parametrize("module", PURE)
def test_pure_modules_name_only_pure_btlab_modules(module):
    tree = ast.parse((BTLAB / f"{module}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "btlab":
            assert all(alias.name in PURE + PURE_AFTER_LOADING for alias in node.names), [a.name for a in node.names]


@pytest.mark.parametrize("module", PURE)
def test_pure_modules_read_no_clock_and_open_no_file(module):
    for node in ast.walk(ast.parse((BTLAB / f"{module}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            assert ast.unparse(node.func) not in FORBIDDEN_CALLS, ast.unparse(node)
