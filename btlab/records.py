"""Records the store reads and writes, shared with the pure core (SPEC.md §5, §6, §7, §18). Pure.

A day is an integer count of days since 1970-01-01; a minute bar is stamped with the UTC epoch second of its start (§3).
Times are aware UTC datetimes. Records that hold arrays are eq=False, so == never compares arrays element by element.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

EPOCH = date(1970, 1, 1)
CATEGORY_SOURCES = ("RULE", "OWNER")
ACTION_KINDS = ("CASH_DIV", "SHARE_RATIO")
COVERAGE_STATUSES = ("OK", "EMPTY", "FAILED", "NO_PERMISSION", "MISMATCH")
EVENT_LEVELS = ("INFO", "WARNING", "ERROR")
EVENT_KINDS = (
    # data
    "UPDATE_STARTED", "UPDATE_FINISHED", "UP_TO_DATE", "SYMBOL_FAILED", "BAD_ACTION", "DIVIDEND_SPLIT_UNKNOWN",
    "TIINGO_SYMBOL_BUDGET", "SPYLEAD_UNREACHABLE", "MINUTE_MISMATCH", "MINUTE_NO_PERMISSION",
    # runs
    "BACKTEST_SAVED", "OPTIMIZATION_SAVED", "JOB_FAILED", "JOB_CANCELLED", "JOB_INTERRUPTED",
    # live
    "BATCH_PROPOSED", "BATCH_CONFIRMED", "BATCH_EXPIRED", "ORDER_SENT", "ORDER_ENDED", "HOLDINGS_MISMATCH",
    "LIVE_DATA_INCOMPLETE", "UNKNOWN_BTLAB_ORDER", "READ_ONLY_API",
    # ibguard
    "EXCLUDED", "PROTECTION_RESTORED", "IBGUARD_RESTORE_FAILED", "THRESHOLDS_WRITTEN",
    # other
    "SETTING_CHANGED", "INTERNAL_ERROR",
)


def to_day(value: date) -> int:
    """Days since 1970-01-01 (§3)."""
    return value.toordinal() - EPOCH.toordinal()


def to_date(day: int) -> date:
    return EPOCH + timedelta(days=int(day))


@dataclass(frozen=True)
class SymbolInfo:
    market: str
    symbol: str
    name: str | None
    category: str
    category_source: str
    exchange: str | None
    minute_enabled: bool
    borrow_fee_pct: float | None
    last_fetch_at: datetime | None
    last_fetch_error: str | None
    added_at: datetime


@dataclass(frozen=True)
class SymbolList:
    id: int
    name: str
    market: str
    created_at: datetime
    symbols: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class DailyBars:
    """One symbol's raw bars in day order. A TW no-trade day has NaN prices (§6.1)."""

    day: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    no_trade: np.ndarray


@dataclass(frozen=True)
class Action:
    """A normalized corporate action (§6.2). ref_before and ref_after belong to TW dividend results."""

    day: int
    kind: str
    detail: str
    cash: float = 0.0
    ratio: float = 1.0
    ref_before: float | None = None
    ref_after: float | None = None


@dataclass(frozen=True)
class ShortSuspension:
    """A TW short-sale suspension from start_day through end_day (§5.2, §10.7)."""

    start_day: int
    end_day: int
    reason: str | None


@dataclass(frozen=True)
class StockInfo:
    """A stock's latest TaiwanStockInfo row (§5.2). exchange is TWSE or TPEX, None for another type."""

    symbol: str
    name: str
    exchange: str | None
    industry: str


@dataclass(frozen=True)
class DividendResult:
    """A TaiwanStockDividendResult row: an ex-date with its reference prices (§6.2). kind is 息, 權 or 權息."""

    day: int
    before_price: float
    after_price: float
    kind: str


@dataclass(frozen=True)
class DividendPolicy:
    """A TaiwanStockDividend row: its cash dividend and the cash ex-date, None when it has none (§6.2)."""

    cash_ex_day: int | None
    cash: float


@dataclass(frozen=True)
class SplitPrice:
    """A TaiwanStockSplitPrice row, a split or reverse split (§6.2)."""

    day: int
    symbol: str
    type: str
    before_price: float
    after_price: float


@dataclass(frozen=True)
class ParValueChange:
    """A TaiwanStockParValueChange row (§6.2)."""

    day: int
    symbol: str
    before_close: float
    after_ref_close: float


@dataclass(frozen=True)
class CapitalReduction:
    """A TaiwanStockCapitalReductionReferencePrice row (§6.2)."""

    day: int
    closing_price: float
    reference_price: float
    reason: str | None


@dataclass(frozen=True, eq=False)
class RiskFreeRates:
    """DTB3 in percent. Days without a value are absent (§5.3)."""

    day: np.ndarray
    rate_pct: np.ndarray


@dataclass(frozen=True)
class Contract:
    """A resolved IB contract (§5.4, §15.2). market_rule_ids is IB's comma-separated text, kept as given."""

    con_id: int
    exchange: str | None
    currency: str
    min_size: float | None
    size_increment: float | None
    market_rule_ids: str | None
    fetched_at: datetime


@dataclass(frozen=True, eq=False)
class ExternalSeries:
    """A spylead series over calendar days from first_day, in original units (§5.6)."""

    name: str
    first_day: int
    values: np.ndarray
    in_sample: bool


@dataclass(frozen=True, eq=False)
class ExternalSnapshot:
    taken_at: datetime
    series: tuple[ExternalSeries, ...]


@dataclass(frozen=True)
class Event:
    id: int
    ts: datetime
    level: str
    kind: str
    market: str | None
    symbol: str | None
    message: str


@dataclass(frozen=True)
class JobRun:
    id: int
    lane: str
    kind: str
    scheduled_for: datetime | None
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    message: str | None


@dataclass(frozen=True, eq=False)
class MinuteBars:
    """One symbol's 1-minute bars of the regular session in raw prices, ordered by t (§6.6)."""

    t: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray


@dataclass(frozen=True)
class MinuteCoverage:
    """The state of one symbol-day of minute bars (§5.4). factor is daily ÷ minute close, when compared."""

    day: int
    status: str
    factor: float | None
    fetched_at: datetime
