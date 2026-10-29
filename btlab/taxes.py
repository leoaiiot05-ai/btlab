"""The Taiwan transaction-tax table, taxes.toml (SPEC.md §10.6). Pure after loading."""

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from btlab.markets import MARKETS

COVERED = ("STOCK", "ETF", "TDR", "BOND_ETF")
DAY_TRADE = "STOCK_DAYTRADE"
CATEGORIES = COVERED + (DAY_TRADE,)
_KEYS = ("category", "rate_pct", "from", "to")
_EPOCH = date(1970, 1, 1)
_FIRST = date(1900, 1, 1)
_LAST = date(9999, 12, 31)


class TaxTableError(Exception):
    """taxes.toml cannot be used. The messages name the entries."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(f"tax table {path}:" + "".join(f"\n  - {problem}" for problem in problems))


@dataclass(frozen=True)
class TaxEntry:
    label: str
    category: str
    rate_pct: float
    first: int
    last: int

    def describe(self) -> str:
        return f"{self.label} ({self.category}, {_iso(self.first)} to {_iso(self.last)})"


@dataclass(frozen=True)
class TaxTable:
    entries: tuple[TaxEntry, ...]

    def rate_pct(self, market: str, category: str, day: int, day_trade: bool = False) -> float:
        """The tax on a sell, in percent of its value. day counts days since 1970-01-01 (SPEC.md §3)."""
        if category not in MARKETS[market].categories:
            raise ValueError(f"{market} has no category {category}")
        if not MARKETS[market].transaction_tax:
            return 0.0
        if day_trade and category == "STOCK":
            entry = self._entry(DAY_TRADE, day)
            if entry is not None:
                return entry.rate_pct
        entry = self._entry(category, day)
        if entry is None:
            raise ValueError(f"no {category} tax rate on day {day}")
        return entry.rate_pct

    def _entry(self, category: str, day: int) -> TaxEntry | None:
        return next((e for e in self.entries if e.category == category and e.first <= day <= e.last), None)


def load_taxes(path: Path) -> TaxTable:
    try:
        # Windows editors may save a BOM, which TOML does not allow.
        doc = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        raise TaxTableError(path, ["the file does not exist"]) from None
    except OSError as error:
        raise TaxTableError(path, [f"cannot be read ({error.strerror or type(error).__name__})"]) from None
    except UnicodeDecodeError:
        raise TaxTableError(path, ["is not UTF-8 text"]) from None
    except tomllib.TOMLDecodeError as error:
        raise TaxTableError(path, [f"is not valid TOML: {error}"]) from None
    return parse_taxes(doc, path)


def parse_taxes(doc: dict[str, Any], path: Path) -> TaxTable:
    """Validate a parsed taxes.toml; path only labels the errors."""
    problems = [f"unknown key {key}" for key in doc if key != "tw"]
    rows = doc.get("tw", [])
    if not isinstance(rows, list):
        raise TaxTableError(path, problems + ["tw must be an array of tables, written [[tw]]"])
    entries = [entry for i, row in enumerate(rows, 1) if (entry := _entry(f"[[tw]] entry {i}", row, problems))]
    # Ranges are checked only when every entry is known; a skipped entry would show up as a false gap.
    if len(entries) == len(rows):
        for category in CATEGORIES:
            same = sorted((e for e in entries if e.category == category), key=lambda e: e.first)
            problems += _overlaps(same)
            if category in COVERED:
                problems += _gaps(category, same)
    if problems:
        raise TaxTableError(path, problems)
    return TaxTable(tuple(entries))


def _entry(label: str, row: Any, problems: list[str]) -> TaxEntry | None:
    if not isinstance(row, dict):
        problems.append(f"{label} must be a table")
        return None
    found = len(problems)
    problems += [f"{label}: unknown key {key}" for key in row if key not in _KEYS]
    category = row.get("category")
    if category is None:
        problems.append(f"{label}: category is missing")
    elif category not in CATEGORIES:
        problems.append(f"{label}: unknown category {category!r}; expected one of {', '.join(CATEGORIES)}")
    rate = row.get("rate_pct")
    if rate is None:
        problems.append(f"{label}: rate_pct is missing")
    elif not _is_number(rate) or not 0 <= rate <= 100:
        problems.append(f"{label}: rate_pct must be a number from 0 to 100")
    first = _date(label, row, "from", _FIRST, problems)
    last = _date(label, row, "to", _LAST, problems)
    if first is not None and last is not None and first > last:
        problems.append(f"{label}: from is after to")
    if len(problems) > found:
        return None
    return TaxEntry(label, category, float(rate), _day(first), _day(last))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _date(label: str, row: dict[str, Any], key: str, default: date, problems: list[str]) -> date | None:
    if key not in row:
        return default
    try:
        return date.fromisoformat(row[key])
    except (TypeError, ValueError):
        problems.append(f'{label}: {key} must be a quoted date "YYYY-MM-DD"')
        return None


def _overlaps(entries: list[TaxEntry]) -> list[str]:
    """entries: one category, sorted by their first day."""
    problems = []
    furthest: TaxEntry | None = None
    for entry in entries:
        if furthest is not None and entry.first <= furthest.last:
            problems.append(f"{furthest.describe()} overlaps {entry.describe()}")
        if furthest is None or entry.last > furthest.last:
            furthest = entry
    return problems


def _gaps(category: str, entries: list[TaxEntry]) -> list[str]:
    """entries: one category, sorted by their first day. They must cover _FIRST to _LAST."""
    if not entries:
        return [f"{category} has no entry; {', '.join(COVERED)} must cover every date"]
    problems = []
    uncovered, before = _day(_FIRST), None
    for entry in entries:
        if entry.first > uncovered:
            problems.append(_gap(category, uncovered, entry.first - 1, before, entry))
        if entry.last >= uncovered:
            uncovered, before = entry.last + 1, entry
    if uncovered <= _day(_LAST):
        problems.append(_gap(category, uncovered, _day(_LAST), before, None))
    return problems


def _gap(category: str, first: int, last: int, before: TaxEntry | None, after: TaxEntry | None) -> str:
    if before is None:
        where = f"before {after.label}"
    elif after is None:
        where = f"after {before.label}"
    else:
        where = f"between {before.label} and {after.label}"
    return f"{category} has no rate from {_iso(first)} to {_iso(last)}, {where}"


def _day(value: date) -> int:
    return (value - _EPOCH).days


def _iso(day: int) -> str:
    return date.fromordinal(day + _EPOCH.toordinal()).isoformat()
