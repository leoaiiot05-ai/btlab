"""Live portfolios (SPEC.md §14): a batch from the replayed backtest, and in test mode the virtual book with its fills,
corporate actions and ledger. Pure.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np

from btlab.calendar import price_limit_pct
from btlab.costs import CostModel
from btlab.markets import MARKETS
from btlab.records import Line, to_date

BATCH_STATUSES = ("PROPOSED", "CONFIRMED", "SENT", "DONE", "EXPIRED", "REPLACED")
SKIPS = ("SKIPPED_TW_SHORT", "SKIPPED_NO_PRICE", "SKIPPED_OWNER")
# Test mode's line states; trade mode adds its own with sending (§15.5).
LINE_STATUSES = ("PROPOSED", *SKIPS, "VIRTUAL_PENDING", "FILLED", "EXPIRED")
TW_LOT = 1000.0
# The last moment to confirm a batch, on its session day (§14.4).
CUTOFFS = {"US": time(9, 20), "TW": time(8, 50)}


@dataclass(frozen=True)
class Book:
    """The virtual book of a test-mode portfolio: cash and shares by symbol (§14.5)."""

    cash: float
    holdings: dict[str, float]

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + sum(quantity * prices[symbol] for symbol, quantity in self.holdings.items())


@dataclass(frozen=True)
class Bar:
    """A symbol's raw bar on a session: lock is +1 locked up, −1 locked down, 0 otherwise (§6.5)."""

    open: float
    high: float
    low: float
    close: float
    tradable: bool
    lock: int


# ----- a batch (§14.2) -----


def replay_targets(holdings: Mapping[str, float], pending: Mapping[str, float]) -> dict[str, float]:
    """The replay's positions after the last close, each pending order's target quantity in its place."""
    return {symbol: float(pending.get(symbol, quantity)) for symbol, quantity in holdings.items()}


def target_weights(targets: Mapping[str, float], prices: Mapping[str, float], equity: float) -> dict[str, float]:
    """w = target quantity × close ÷ replay equity, on the replay's own valuation prices."""
    return {symbol: quantity * prices[symbol] / equity if quantity else 0.0 for symbol, quantity in targets.items()}


def live_lot(market: str, size_increment: float | None) -> float:
    """US trades whole shares; TW the contract's size increment when IB gave one, else a board lot (§14.2, §15.2)."""
    return 1.0 if market == "US" else (size_increment or TW_LOT)


def target_quantity(weight: float, equity: float, close: float, lot: float) -> float:
    """w × live equity ÷ close, toward zero to the lot."""
    return math.trunc(weight * equity / close / lot) * lot


def batch_lines(market: str, weights: Mapping[str, float], closes: Mapping[str, float | None],
                current: Mapping[str, float], equity: float, lots: Mapping[str, float], session_day: int,
                us_order_kind: str, offset_pct: float) -> list[Line]:
    """Steps 4 to 6 of §14.2, symbol by symbol: Δ = target − current, lines under one lot dropped, and the skips."""
    lines = []
    for symbol, weight in weights.items():
        held = current.get(symbol, 0.0)
        close = closes.get(symbol)
        if close is None:
            if weight or held:
                lines.append(Line(symbol, "BUY" if weight > 0 else "SELL", 0.0, _kind(market, us_order_kind), None, 0.0,
                                  "SKIPPED_NO_PRICE", "no close is stored"))
            continue
        change = target_quantity(weight, equity, close, lots[symbol]) - held
        if abs(change) < lots[symbol]:
            continue
        action = "BUY" if change > 0 else "SELL"
        kind = _kind(market, us_order_kind)
        limit = None if kind == "MOO" else limit_price(market, action, close, offset_pct, session_day)
        if market == "TW" and action == "SELL" and held + change < 0:
            line = Line(symbol, action, abs(change), kind, limit, close, "SKIPPED_TW_SHORT", "TW lines never open a short")
        else:
            line = Line(symbol, action, abs(change), kind, limit, close, "PROPOSED")
        lines.append(line)
    return lines


def _kind(market: str, us_order_kind: str) -> str:
    return us_order_kind if market == "US" else "LIMIT"


def limit_price(market: str, action: str, reference: float, offset_pct: float, session_day: int) -> float:
    """The reference ± the offset, plus for buys and minus for sells; TW inside the session's price limits (§15.4)."""
    sign = 1 if action == "BUY" else -1
    price = reference * (1 + sign * offset_pct / 100)
    if market != "TW":
        return price
    limit = float(price_limit_pct(market, np.array([session_day]))[0]) / 100
    return min(max(price, reference * (1 - limit)), reference * (1 + limit))


def cutoff(market: str, session_day: int) -> datetime:
    """The confirmation cutoff of a batch: 09:20 New York for US, 08:50 Taipei for TW, on its session day (§14.4)."""
    return datetime.combine(to_date(session_day), CUTOFFS[market], ZoneInfo(MARKETS[market].time_zone))


# ----- the virtual book (§14.5) -----


def corporate_actions(book: Book, ratios: Mapping[str, float], dividends: Mapping[str, float],
                      previous_closes: Mapping[str, float], withholding_pct: float) -> Book:
    """The day's actions at its open, by the REALISTIC rules (§10.7): a share ratio r makes trunc(q × r) shares and
    settles the remainder in cash at the previous raw close ÷ r; a cash dividend D pays longs q × D net of withholding
    and charges shorts |q| × D."""
    cash, holdings = book.cash, dict(book.holdings)
    for symbol, quantity in book.holdings.items():
        ratio = ratios.get(symbol, 1.0)
        if quantity and ratio != 1.0:
            whole = float(math.trunc(quantity * ratio))
            cash += (quantity * ratio - whole) * previous_closes[symbol] / ratio
            holdings[symbol] = quantity = whole
        dividend = dividends.get(symbol, 0.0)
        if quantity and dividend:
            cash += quantity * dividend * ((1 - withholding_pct / 100) if quantity > 0 else 1)
    return Book(cash, holdings)


def fill_price(line: Line, bar: Bar) -> float | None:
    """The price a confirmed line fills at on its session's bar by the backtest rules, before slippage, or None when
    it expires: an untradable bar, a lock against its side, or a limit the bar never reached. Day orders never carry
    over."""
    buy = line.action == "BUY"
    if not bar.tradable or (bar.lock > 0 and buy) or (bar.lock < 0 and not buy):
        return None
    if line.kind == "MOO":
        return bar.open
    limit = line.limit_price
    if buy:
        return bar.open if bar.open <= limit else (limit if bar.low <= limit else None)
    return bar.open if bar.open >= limit else (limit if bar.high >= limit else None)


def settle(book: Book, line: Line, price: float, costs: CostModel, symbol_index: int, day: int) -> tuple[Book, float, float]:
    """The book after a fill at price, with slippage (never past a limit) and the costs of the stored configuration;
    returns the book, the fill price and the costs."""
    buy = line.action == "BUY"
    paid = costs.fill_price(price, buy)
    if line.kind == "LIMIT":
        paid = min(paid, line.limit_price) if buy else max(paid, line.limit_price)
    held = book.holdings.get(line.symbol, 0.0)
    value = line.quantity * paid
    charge = costs.commission(line.quantity, value)
    if not buy:
        charge += costs.tax_rate(symbol_index, day) * value / 100
        if held - line.quantity < 0:
            charge += costs.short_sale_fee(value)
    change = line.quantity if buy else -line.quantity
    holdings = {**book.holdings, line.symbol: held + change}
    cash = book.cash - value - charge if buy else book.cash + value - charge
    return Book(cash, {symbol: quantity for symbol, quantity in holdings.items() if quantity}), paid, charge
