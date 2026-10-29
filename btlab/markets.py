"""Market constants (SPEC.md §4.1). Pure."""

from dataclasses import dataclass
from datetime import date, time

# The first update of a market fixes data_start this many years back: 30 years plus a year of warm-up.
DATA_START_YEARS = 31


@dataclass(frozen=True)
class Session:
    """A regular trading session in the market's local time."""

    open: time
    close: time


@dataclass(frozen=True)
class PriceLimit:
    """A daily price limit of ±pct percent, in force from start (inclusive) until the next one."""

    start: date
    pct: float


@dataclass(frozen=True)
class PerShareCommission:
    """IBKR Pro Fixed: min(max(per_share × q, min), max_pct% × value)."""

    per_share: float
    min: float
    max_pct: float


@dataclass(frozen=True)
class RateCommission:
    """max(rate_pct% × value, min)."""

    rate_pct: float
    min: float


@dataclass(frozen=True)
class Market:
    code: str
    currency: str
    time_zone: str
    session: Session
    categories: tuple[str, ...]
    benchmark: str
    initial_capital: float
    lot: int
    board_lot: int | None
    price_limits: tuple[PriceLimit, ...]
    commission: PerShareCommission | RateCommission
    transaction_tax: bool
    slippage_bps: float
    short_collateral: float
    borrow_fee_pct: float | None
    short_fee_pct: float | None
    dividend_withholding_pct: float
    # Monday is 0, in Asia/Taipei; the time of day is [schedule] in config.toml.
    update_weekdays: tuple[int, ...]
    order_kinds: tuple[str, ...]


US = Market(
    code="US",
    currency="USD",
    time_zone="America/New_York",
    session=Session(time(9, 30), time(16, 0)),
    categories=("STOCK", "ETF"),
    benchmark="SPY",
    initial_capital=100_000.0,
    lot=1,
    board_lot=None,
    price_limits=(),
    commission=PerShareCommission(per_share=0.005, min=1.0, max_pct=1.0),
    transaction_tax=False,
    slippage_bps=5.0,
    short_collateral=0.5,
    borrow_fee_pct=0.5,
    short_fee_pct=None,
    dividend_withholding_pct=30.0,
    update_weekdays=(1, 2, 3, 4, 5),
    order_kinds=("MOO", "LIMIT"),
)

TW = Market(
    code="TW",
    currency="TWD",
    time_zone="Asia/Taipei",
    session=Session(time(9, 0), time(13, 30)),
    categories=("STOCK", "ETF", "BOND_ETF", "TDR"),
    benchmark="0050",
    initial_capital=1_000_000.0,
    lot=1,
    board_lot=1_000,
    price_limits=(PriceLimit(date.min, 7.0), PriceLimit(date(2015, 6, 1), 10.0)),
    commission=RateCommission(rate_pct=0.08, min=80.0),
    transaction_tax=True,
    slippage_bps=10.0,
    short_collateral=0.9,
    borrow_fee_pct=None,
    short_fee_pct=0.08,
    dividend_withholding_pct=0.0,
    update_weekdays=(0, 1, 2, 3, 4),
    order_kinds=("LIMIT",),
)

MARKETS = {market.code: market for market in (US, TW)}
