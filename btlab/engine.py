"""The simulation loop and portfolio accounting (SPEC.md §10). Pure.

One loop serves daily and minute timelines: corporate actions, borrow fees and day trades key on each bar's session
and the session's day. Each bar runs the steps of §10.3 in order; step 3, intrabar exits, is a hook.
"""

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from btlab.costs import CostModel
from btlab.indicators import atr as average_true_range
from btlab.markets import MARKETS
from btlab.prices import Aligned
from btlab.records import ShortSuspension, Trade
from btlab.sizing import Sizing, cap, entry_quantity, to_lot

# A weight that moved less than this is unchanged (§9.1).
WEIGHT_TOLERANCE = 1e-9
PRICE_MODES = ("REALISTIC", "ADJUSTED")
FILL_TIMINGS = ("NEXT_OPEN", "SAME_CLOSE")
# A position that a share event leaves below one share is settled in cash entirely (§10.7).
EXIT_REASONS = ("SIGNAL", "FORCED_COVER", "CASH_IN_LIEU", "OPEN")
_BISECTIONS = 60


@dataclass(frozen=True, eq=False)
class Timeline:
    """Bars × symbols of one run (§10.3, §11.2). time holds day numbers on the daily timeline and UTC epoch seconds on
    minute timelines; day is the day of each bar's session; session is a non-decreasing session index. Prices are raw,
    adj_* adjusted (§6.3). cash and ratio are the corporate actions on a session's first bar. Trading and equity start
    at bar `start` (§10.1)."""

    symbols: tuple[str, ...]
    time: np.ndarray
    day: np.ndarray
    session: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    adj_open: np.ndarray
    adj_high: np.ndarray
    adj_low: np.ndarray
    adj_close: np.ndarray
    tradable: np.ndarray
    lock: np.ndarray
    cash: np.ndarray
    ratio: np.ndarray
    start: int


def daily_timeline(aligned: Aligned, start: int) -> Timeline:
    """The daily timeline: each bar is its own session."""
    return Timeline(
        symbols=aligned.symbols, time=aligned.days, day=aligned.days, session=np.arange(len(aligned.days)),
        open=aligned.open, high=aligned.high, low=aligned.low, close=aligned.close,
        adj_open=aligned.adj_open, adj_high=aligned.adj_high, adj_low=aligned.adj_low, adj_close=aligned.adj_close,
        tradable=aligned.tradable, lock=aligned.lock, cash=aligned.cash, ratio=aligned.ratio, start=start,
    )


@dataclass(frozen=True, eq=False)
class Targets:
    """DIRECTION (+1, 0, −1) or WEIGHTS (fractions of equity) per bar and symbol; NaN is no instruction (§9.1).
    rebalance marks the bars on which every WEIGHTS target executes."""

    kind: str
    values: np.ndarray
    rebalance: np.ndarray | None = None


class OrderStrategy(Protocol):
    """An event-driven strategy (ORDERS): it places orders through the book at each bar's close."""

    def on_bar(self, book: "Book") -> None: ...


class Exits(Protocol):
    """Exit overlays (§10.8): intrabar is step 3 of each bar; at_close turns due max-hold exits into orders, after the
    strategy's evaluation, so that they replace its orders for those symbols."""

    def intrabar(self, simulation: "Simulation", t: int) -> None: ...

    def at_close(self, simulation: "Simulation", t: int) -> None: ...


@dataclass(frozen=True)
class Settings:
    """A run's execution settings (§10.1). suspensions holds each symbol's TW short-sale suspensions."""

    market: str
    initial_capital: float
    costs: CostModel
    sizing: Sizing = Sizing()
    price_mode: str = "REALISTIC"
    fill_timing: str = "NEXT_OPEN"
    allow_short: bool = False
    board_lots_only: bool = False
    max_position_pct: float = 100.0
    suspensions: tuple[tuple[ShortSuspension, ...], ...] = ()


@dataclass(frozen=True)
class Fill:
    time: int
    symbol: str
    side: str
    quantity: float
    price: float
    costs: float
    reason: str


@dataclass(frozen=True, eq=False)
class EngineResult:
    """The run from bar `start` on (§10.11, without metrics). quantity holds the positions after each bar's close;
    holdings and pending are the final positions and pending targets (symbol index → quantity). flags maps each flag
    raised to its detail, None or PATH_FALLBACK's count."""

    time: np.ndarray
    equity: np.ndarray
    cash: np.ndarray
    long_value: np.ndarray
    short_value: np.ndarray
    gross: np.ndarray
    net: np.ndarray
    positions: np.ndarray
    quantity: np.ndarray
    trades: tuple[Trade, ...]
    fills: tuple[Fill, ...]
    flags: dict[str, int | None]
    holdings: np.ndarray
    pending: dict[int, float]


def simulate(timeline: Timeline, strategy: "Targets | OrderStrategy", settings: Settings, exits: Exits | None = None) -> EngineResult:
    """Runs a strategy over a timeline (§10.3). Deterministic: equal inputs give bit-identical results (§10.10)."""
    return Simulation(timeline, strategy, settings, exits).run()


@dataclass
class _Order:
    target: float
    reason: str = "SIGNAL"
    signal_bar: int = 0


@dataclass
class OpenTrade:
    """Average-cost bookkeeping of one open trade; side is +1 (long) or −1 (short). The exit levels (§10.8) are set
    by the overlays: the trailing extreme (peak or trough), ibguard's ref and prev, and the ATR stop's distance."""

    side: int
    entry_time: int
    entry_bar: int = 0
    signal_bar: int = 0
    entered: float = 0.0
    entered_value: float = 0.0
    average: float = 0.0
    realized: float = 0.0
    exited: float = 0.0
    exited_value: float = 0.0
    costs: float = 0.0
    dividends: float = 0.0
    extreme: float | None = None
    ref: float | None = None
    prev: float | None = None
    atr_distance: float | None = None

    def add(self, quantity: float, price: float, costs: float, held: float) -> None:
        self.average = (self.average * held + price * quantity) / (held + quantity)
        self.entered += quantity
        self.entered_value += quantity * price
        self.costs += costs

    def reduce(self, quantity: float, price: float, costs: float) -> None:
        self.realized += (price - self.average) * quantity * self.side
        self.exited += quantity
        self.exited_value += quantity * price
        self.costs += costs

    def scale(self, ratio: float) -> None:
        """A share event: r new shares per old share; every price level is divided by r (§10.7)."""
        self.average /= ratio
        self.entered *= ratio
        self.exited *= ratio
        for name in ("extreme", "ref", "prev", "atr_distance"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, value / ratio)

    def close(self, symbol: str, exit_time: int, reason: str, exit_price: float | None = None, unrealized: float = 0.0) -> Trade:
        """exit_price defaults to the average price of the exits."""
        pnl = self.realized + unrealized + self.dividends - self.costs
        return Trade(
            symbol=symbol, side="LONG" if self.side > 0 else "SHORT", quantity=float(self.entered),
            entry_time=self.entry_time, entry_price=float(self.entered_value / self.entered) if self.entered else 0.0,
            exit_time=exit_time, exit_price=float(self.exited_value / self.exited if exit_price is None else exit_price),
            pnl=float(pnl), return_pct=float(pnl / self.entered_value * 100) if self.entered_value else 0.0,
            costs=float(self.costs), exit_reason=reason,
        )


class Book:
    """What an event-driven strategy sees and does at a bar's close (§9.1, §9.5). Targets are rounded toward zero to
    the lot; the last order for a symbol in a bar wins."""

    def __init__(self, simulation: "Simulation", t: int) -> None:
        self._simulation = simulation
        self.t = t
        self.time = int(simulation.timeline.time[t])
        self.targets: dict[int, float] = {}

    @property
    def positions(self) -> np.ndarray:
        view = self._simulation.q.view()
        view.flags.writeable = False
        return view

    @property
    def equity(self) -> float:
        return self._simulation.equity

    @property
    def cash(self) -> float:
        return self._simulation.cash

    def price(self, j: int) -> float:
        """The symbol's close at this bar in the run's price mode; NaN without data."""
        return float(self._simulation.close[self.t, j])

    def order_target_qty(self, j: int, quantity: float) -> None:
        self.targets[j] = to_lot(quantity, self._simulation.lot)

    def order_qty(self, j: int, change: float) -> None:
        self.order_target_qty(j, self._simulation.q[j] + change)

    def order_target_weight(self, j: int, weight: float) -> None:
        self.order_target_qty(j, weight * self.equity / self.price(j))

    def order_value(self, j: int, amount: float) -> None:
        self.order_qty(j, amount / self.price(j))


class Simulation:
    """The state of one run. Exit overlays act on it through the Exits hook: they read the prices of the run's price
    mode, the positions and open trades, and call exit_position and order."""

    def __init__(self, timeline: Timeline, strategy: "Targets | OrderStrategy", settings: Settings, exits: Exits | None) -> None:
        if settings.price_mode not in PRICE_MODES or settings.fill_timing not in FILL_TIMINGS:
            raise ValueError("unknown price mode or fill timing")
        self.timeline, self.strategy, self.settings, self.exits = timeline, strategy, settings, exits
        self.costs = settings.costs
        market = MARKETS[settings.market]
        self.lot = market.board_lot if settings.board_lots_only else market.lot
        self.margin = market.short_collateral
        self.realistic = settings.price_mode == "REALISTIC"
        self.open = timeline.open if self.realistic else timeline.adj_open
        self.high = timeline.high if self.realistic else timeline.adj_high
        self.low = timeline.low if self.realistic else timeline.adj_low
        self.close = timeline.close if self.realistic else timeline.adj_close
        self.t = timeline.start
        count = len(timeline.symbols)
        self.cash = float(settings.initial_capital)
        self.equity = self.cash
        self.q = np.zeros(count)
        self.price = self._prices_before_start()
        self.pending: dict[int, _Order] = {}
        self.forced: set[int] = set()
        self.open_trades: dict[int, OpenTrade] = {}
        self.trades: list[Trade] = []
        self.fills: list[Fill] = []
        self.flags: dict[str, int | None] = {}
        self.instructed = np.zeros(count)
        self.executed = np.full(count, np.nan)
        self.bought, self.shorted, self.shorted_value = np.zeros(count), np.zeros(count), np.zeros(count)
        self.suspended, self.cover_due = self._suspensions()
        self.atr = self._atr()
        self._moment_quantity: dict[int, float] = {}
        self._moment_value: dict[int, float] = {}
        self._records: list[tuple] = []

    # ----- the loop (§10.3) -----

    def run(self) -> EngineResult:
        timeline = self.timeline
        for t in range(timeline.start, len(timeline.time)):
            self.t = t
            if t == timeline.start or timeline.session[t] != timeline.session[t - 1]:
                self._start_session(t)
            self._fill_at_open(t)
            if self.exits is not None:
                self.exits.intrabar(self, t)
            self._close(t)
        return self._result()

    def _start_session(self, t: int) -> None:
        """Step 1: corporate actions, due TW forced covers, borrow fees, and a fresh day-trade record."""
        timeline = self.timeline
        # Borrow fees are charged on the short value at the previous close, before the day's actions change q.
        days = int(timeline.day[t] - timeline.day[t - 1]) if t > 0 else 0
        for j in np.flatnonzero(self.q < 0):
            fee = self.costs.borrow_fee(j, -self.q[j] * self.price[j], days)
            if fee:
                self.cash -= fee
                self.open_trades[j].costs += fee
        if self.realistic:
            for j in np.flatnonzero((timeline.ratio[t] != 1) | (timeline.cash[t] != 0)):
                self._corporate_action(t, j, float(timeline.ratio[t, j]), float(timeline.cash[t, j]))
        self.forced.update(np.flatnonzero(self.cover_due[t] & (self.q < 0)).tolist())
        self.bought[:] = self.shorted[:] = self.shorted_value[:] = 0.0

    def _fill_at_open(self, t: int) -> None:
        """Step 2: forced covers always fill at the open; pending orders too under NEXT_OPEN."""
        if self.forced:
            covers = {j: _Order(0.0, "FORCED_COVER", t) for j in sorted(self.forced)}
            self.fill(t, self.open[t], covers)
            self.forced = {j for j in self.forced if self.q[j] < 0}
        if self.settings.fill_timing == "NEXT_OPEN" and self.pending:
            self.fill(t, self.open[t], self.pending)

    def _close(self, t: int) -> None:
        """Step 4: value and record equity, the strategy's orders and the due max-hold exits, which replace the
        strategy's orders for their symbols, then SAME_CLOSE fills."""
        self._value(t)
        self._evaluate(t)
        if self.exits is not None:
            self.exits.at_close(self, t)
        if self.settings.fill_timing == "SAME_CLOSE" and self.pending:
            self.fill(t, self.close[t], self.pending)
            self._value(t)
        long_value, short_value = self._sides()
        self._records.append((self.timeline.time[t], self.equity, self.cash, long_value, short_value, self.q.copy()))

    # ----- corporate actions (§10.7) -----

    def _corporate_action(self, t: int, j: int, ratio: float, cash: float) -> None:
        # Tiingo quotes a US dividend per post-split share; a TW dividend result pays D per old share (§6.2, §6.3).
        if self.settings.market == "TW":
            self._dividend(j, cash)
            self._share_event(t, j, ratio)
        else:
            self._share_event(t, j, ratio)
            self._dividend(j, cash)

    def _share_event(self, t: int, j: int, ratio: float) -> None:
        """trunc(q × r) shares; the remainder in cash at the previous raw close ÷ r; pending targets × r."""
        if ratio == 1:
            return
        order = self.pending.get(j)
        if order is not None:
            order.target = to_lot(order.target * ratio, self.lot)
        if self.q[j] == 0:
            return
        exact = self.q[j] * ratio
        whole = float(np.trunc(exact))
        price = self.price[j] / ratio
        trade = self.open_trades[j]
        trade.scale(ratio)
        trade.realized += (price - trade.average) * (exact - whole)
        self.cash += (exact - whole) * price
        self.q[j] = whole
        if whole == 0:
            self._end_trade(j, t, "CASH_IN_LIEU", price)

    def _dividend(self, j: int, cash: float) -> None:
        """Longs receive q × D less withholding; shorts pay |q| × D."""
        if cash == 0 or self.q[j] == 0:
            return
        amount = self.q[j] * cash
        flow = self.costs.net_dividend(amount) if amount > 0 else amount
        self.cash += flow
        self.open_trades[j].dividends += flow

    # ----- strategy outputs (§9.1, §9.4) -----

    def _evaluate(self, t: int) -> None:
        strategy = self.strategy
        if isinstance(strategy, Targets):
            if strategy.kind == "DIRECTION":
                self._directions(t, strategy.values[t])
            elif strategy.kind == "WEIGHTS":
                self._weights(t, strategy.values[t], bool(strategy.rebalance[t]) if strategy.rebalance is not None else False)
            else:
                raise ValueError(f"unknown target kind {strategy.kind}")
            return
        book = Book(self, t)
        strategy.on_bar(book)
        targets = self._long_only(np.array(list(book.targets.values()), dtype=np.float64))
        for j, target in zip(book.targets, targets):
            self.order(j, target)

    def _long_only(self, values: np.ndarray) -> np.ndarray:
        """Negative targets count as 0 without allow_short, and the result says so (§9.1)."""
        if self.settings.allow_short or not (values < 0).any():
            return values
        self.flags["SHORTS_IGNORED"] = None
        return np.where(values < 0, 0.0, values)

    def _directions(self, t: int, values: np.ndarray) -> None:
        """A direction that differs from the last one instructed enters, exits or reverses; quantities are held."""
        direction = self._long_only(np.sign(values))
        for j in np.flatnonzero(~np.isnan(direction) & (direction != self.instructed)):
            if direction[j] == 0:
                target = 0.0
            else:
                price = self.close[t, j]
                if not np.isfinite(price):
                    continue
                target = direction[j] * self._entry_size(t, j, price)
            self.instructed[j] = direction[j]
            self.order(j, target)

    def _entry_size(self, t: int, j: int, price: float) -> float:
        """§9.4, at the signal bar's equity and close; the ATR is converted to the raw scale in REALISTIC mode."""
        settings = self.settings
        atr = self.atr[t, j] if self.atr is not None else np.nan
        if self.atr is not None and self.realistic:
            atr *= self.timeline.close[t, j] / self.timeline.adj_close[t, j]
        quantity = entry_quantity(settings.sizing, self.equity, price, atr, len(self.timeline.symbols))
        return to_lot(cap(quantity, self.equity, price, settings.max_position_pct), self.lot)

    def _weights(self, t: int, values: np.ndarray, rebalance: bool) -> None:
        """On a rebalance bar every target executes; otherwise those that moved from the last executed one."""
        weights = self._long_only(values)
        given = ~np.isnan(weights)
        moved = given & ~(np.abs(weights - self.executed) <= WEIGHT_TOLERANCE)
        for j in np.flatnonzero(given if rebalance else moved):
            price = self.close[t, j]
            if not np.isfinite(price):
                continue
            self.executed[j] = weights[j]
            self.order(j, to_lot(weights[j] * self.equity / price, self.lot))

    def order(self, j: int, target: float, reason: str = "SIGNAL") -> None:
        """A target quantity for a symbol; it replaces the symbol's pending order. An order smaller than one lot is
        skipped, but an order to 0 always closes the whole position."""
        if target == self.q[j] or (target != 0 and abs(target - self.q[j]) < self.lot):
            self.pending.pop(j, None)
            return
        self.pending[j] = _Order(float(target), reason, self.t)

    def exit_position(self, t: int, j: int, price: float, reason: str) -> None:
        """Closes a whole position at a price, as an intrabar exit does; when it cannot fill there, it stays pending
        with its reason. It replaces the symbol's pending order."""
        self.pending.pop(j, None)
        prices = np.full(len(self.q), np.nan)
        prices[j] = price
        exit_order = {j: _Order(0.0, reason, t)}
        self.fill(t, prices, exit_order)
        self.pending.update(exit_order)

    # ----- fills (§10.5) -----

    def fill(self, t: int, prices: np.ndarray, orders: dict[int, _Order]) -> None:
        """One fill moment: reducing legs of every symbol first, then increasing legs within the buying power, scaled
        together when short. A blocked leg keeps its order pending; a scaled order is done."""
        self._moment_quantity, self._moment_value = {}, {}
        increases: list[tuple[int, float]] = []
        blocked: set[int] = set()
        for j in sorted(orders):
            reduce, increase = self._legs(j, orders[j].target)
            if reduce:
                if self._can_fill(t, j, buy=reduce > 0, increasing=False):
                    self._execute(t, j, reduce, prices[j], orders[j], increasing=False)
                else:
                    blocked.add(j)
            if increase:
                if self._can_fill(t, j, buy=increase > 0, increasing=True) and j not in blocked:
                    increases.append((j, increase))
                else:
                    blocked.add(j)
        scale = self._scale(t, prices, increases)
        for j, increase in increases:
            quantity = np.sign(increase) * to_lot(abs(increase) * scale, self.lot)
            if quantity:
                self._execute(t, j, quantity, prices[j], orders[j], increasing=True)
        for j in list(orders):
            if j not in blocked or self.q[j] == orders[j].target:
                orders.pop(j)

    def _legs(self, j: int, target: float) -> tuple[float, float]:
        """The signed change toward 0 and the signed change beyond it, each a whole number of lots unless closing."""
        current = self.q[j]
        delta = target - current
        reduce = 0.0
        if current and np.sign(delta) == -np.sign(current):
            reduce = -np.sign(current) * min(abs(delta), abs(current))
        increase = delta - reduce
        if target != 0:
            reduce = np.sign(reduce) * to_lot(abs(reduce), self.lot)
        return float(reduce), float(np.sign(increase) * to_lot(abs(increase), self.lot))

    def _can_fill(self, t: int, j: int, buy: bool, increasing: bool) -> bool:
        """Not on an untradable bar (§6.5), nor against a limit lock, nor a TW short sale inside a suspension."""
        timeline = self.timeline
        if not timeline.tradable[t, j]:
            return False
        if timeline.lock[t, j] == (1 if buy else -1):
            return False
        return not (increasing and not buy and self.suspended[t, j])

    def _scale(self, t: int, prices: np.ndarray, increases: list[tuple[int, float]]) -> float:
        """The common factor that fits the increasing legs into the buying power, with slippage and costs."""
        if not increases:
            return 1.0
        power = self._buying_power(prices)

        def need(factor: float) -> float:
            total = 0.0
            for j, increase in increases:
                quantity = to_lot(abs(increase) * factor, self.lot)
                if quantity:
                    price = self.costs.fill_price(prices[j], buy=increase > 0)
                    charge = self._leg_costs(t, j, quantity, price, buy=increase > 0, increasing=True)
                    total += quantity * price * (1 if increase > 0 else self.margin) + charge
            return total

        if need(1.0) <= power:
            return 1.0
        low, high = 0.0, 1.0
        for _ in range(_BISECTIONS):
            middle = (low + high) / 2
            low, high = (middle, high) if need(middle) <= power else (low, middle)
        return low

    def _buying_power(self, prices: np.ndarray) -> float:
        """BP = E − long value − m × |short value| = cash − (1 + m) × |short value|, at the fill moment's prices."""
        now = np.where(np.isfinite(prices), prices, self.price)
        short = float(-(self.q[self.q < 0] * now[self.q < 0]).sum())
        return self.cash - (1 + self.margin) * short

    def _leg_costs(self, t: int, j: int, quantity: float, price: float, buy: bool, increasing: bool) -> float:
        """The commission this leg adds to the symbol's fill, the transaction tax and the TW short-sale fee."""
        value = quantity * price
        before_quantity, before_value = self._moment_quantity.get(j, 0.0), self._moment_value.get(j, 0.0)
        charge = self.costs.commission(before_quantity + quantity, before_value + value) - self.costs.commission(
            before_quantity, before_value
        )
        day = int(self.timeline.day[t])
        if not buy and not increasing:
            day_trade = min(quantity, self.bought[j])
            charge += (self.costs.tax_rate(j, day, True) * day_trade + self.costs.tax_rate(j, day) * (quantity - day_trade)) * price / 100
        elif not buy:
            charge += self.costs.tax_rate(j, day) * value / 100 + self.costs.short_sale_fee(value)
        elif not increasing and self.shorted[j]:
            day_trade = min(quantity, self.shorted[j])
            average = self.shorted_value[j] / self.shorted[j]
            charge -= (self.costs.tax_rate(j, day) - self.costs.tax_rate(j, day, True)) * day_trade * average / 100
        return charge

    def _execute(self, t: int, j: int, change: float, raw: float, order: _Order, increasing: bool) -> None:
        reason = order.reason
        buy = change > 0
        quantity = abs(change)
        price = self.costs.fill_price(raw, buy)
        charge = self._leg_costs(t, j, quantity, price, buy, increasing)
        self._moment_quantity[j] = self._moment_quantity.get(j, 0.0) + quantity
        self._moment_value[j] = self._moment_value.get(j, 0.0) + quantity * price
        self._day_trades(j, quantity, price, buy, increasing)
        self.cash += -quantity * price - charge if buy else quantity * price - charge
        held = abs(self.q[j])
        self.q[j] += change
        time = int(self.timeline.time[t])
        side = "BUY" if buy else "SELL"
        self.fills.append(Fill(time, self.timeline.symbols[j], side, float(quantity), float(price), float(charge), reason))
        if increasing:
            if j not in self.open_trades:
                self.open_trades[j] = OpenTrade(1 if buy else -1, time, entry_bar=t, signal_bar=order.signal_bar)
            self.open_trades[j].add(quantity, price, charge, held)
            return
        self.open_trades[j].reduce(quantity, price, charge)
        if self.q[j] == 0:
            self._end_trade(j, t, reason)

    def _day_trades(self, j: int, quantity: float, price: float, buy: bool, increasing: bool) -> None:
        """This session's buys still held and its short sales still open, for the day-trade rates (§10.6)."""
        if buy and increasing:
            self.bought[j] += quantity
        elif not buy and not increasing:
            self.bought[j] -= min(quantity, self.bought[j])
        elif not buy:
            self.shorted[j] += quantity
            self.shorted_value[j] += quantity * price
        elif self.shorted[j]:
            covered = min(quantity, self.shorted[j])
            self.shorted_value[j] -= covered * self.shorted_value[j] / self.shorted[j]
            self.shorted[j] -= covered

    def _end_trade(self, j: int, t: int, reason: str, price: float | None = None) -> None:
        trade = self.open_trades.pop(j)
        self.trades.append(trade.close(self.timeline.symbols[j], int(self.timeline.time[t]), reason, price))

    # ----- valuation and the result -----

    def _value(self, t: int) -> None:
        """Positions at the close; a symbol without data keeps its last close (§10.5)."""
        prices = self.close[t]
        known = np.isfinite(prices)
        self.price[known] = prices[known]
        long_value, short_value = self._sides()
        self.equity = self.cash + long_value - short_value

    def _sides(self) -> tuple[float, float]:
        values = self.q * self.price
        return float(values[values > 0].sum()), float(-values[values < 0].sum())

    def _prices_before_start(self) -> np.ndarray:
        """Each symbol's last close before the first trading bar, 0 when it has none (it holds nothing then)."""
        earlier = self.close[: self.timeline.start]
        price = np.zeros(len(self.timeline.symbols))
        for j in range(len(price)):
            known = np.flatnonzero(np.isfinite(earlier[:, j]))
            if len(known):
                price[j] = earlier[known[-1], j]
        return price

    def _suspensions(self) -> tuple[np.ndarray, np.ndarray]:
        """Bars inside a TW short-sale suspension, and the first bar on or after each suspension's start."""
        days = self.timeline.day
        shape = self.close.shape
        suspended, cover_due = np.zeros(shape, dtype=bool), np.zeros(shape, dtype=bool)
        for j, spans in enumerate(self.settings.suspensions):
            for span in spans:
                suspended[:, j] |= (days >= span.start_day) & (days <= span.end_day)
                first = int(np.searchsorted(days, span.start_day, side="left"))
                if first < shape[0]:
                    cover_due[first, j] = True
        return suspended, cover_due

    def _atr(self) -> np.ndarray | None:
        sizing = self.settings.sizing
        if sizing.mode != "VOL_RISK":
            return None
        timeline = self.timeline
        return average_true_range(timeline.adj_high, timeline.adj_low, timeline.adj_close, sizing.atr_n)

    def _result(self) -> EngineResult:
        timeline = self.timeline
        last = len(timeline.time) - 1
        for j in sorted(self.open_trades):
            trade = self.open_trades[j]
            unrealized = (self.price[j] - trade.average) * abs(self.q[j]) * trade.side
            self.trades.append(trade.close(timeline.symbols[j], int(timeline.time[last]), "OPEN", float(self.price[j]), unrealized))
            if not np.isfinite(self.close[last, j]):
                self.flags["DATA_ENDED"] = None
        columns = list(zip(*self._records)) if self._records else [()] * 6
        time, equity, cash, long_value, short_value, quantity = columns
        equity, long_value, short_value = (np.array(c, dtype=np.float64) for c in (equity, long_value, short_value))
        quantity = np.array(quantity, dtype=np.float64).reshape(len(equity), len(timeline.symbols))
        with np.errstate(divide="ignore", invalid="ignore"):
            gross = np.where(equity > 0, (long_value + short_value) / equity, np.nan)
            net = np.where(equity > 0, (long_value - short_value) / equity, np.nan)
        return EngineResult(
            time=np.array(time, dtype=np.int64), equity=equity, cash=np.array(cash, dtype=np.float64),
            long_value=long_value, short_value=short_value, gross=gross, net=net,
            positions=(quantity != 0).sum(axis=1), quantity=quantity, trades=tuple(self.trades), fills=tuple(self.fills),
            flags=dict(self.flags), holdings=self.q.copy(),
            pending={j: order.target for j, order in sorted(self.pending.items())},
        )
