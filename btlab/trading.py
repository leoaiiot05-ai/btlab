"""Trade mode (SPEC.md §14, §15, §16): batches from IB's positions, sending with ibguard's protocol around sells,
following each order to its end, recovery, cancelling btlab's own orders, the sweeps, and the ledger.

This module sees IB only through the OrderPort and AccountPort protocols and the dataclasses below: no ib_async type
crosses into it (§15.1). Only ib_orders.py places or cancels orders.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

import numpy as np

from btlab.config import Limits
from btlab.http import SourceError
from btlab.ibguard_client import EXITING, IbguardClient
from btlab.live import ENDED, apply_limits, batch_lines, cutoff, live_lot, rule_price
from btlab.notifier import Notifier
from btlab.records import Contract, Exclusion, LedgerRow, Line, LiveBatch, LiveOrder, LivePortfolio, to_date
from btlab.store import Store

PREFIX = "btlab:"
READ_ONLY_CODE = 321
CANCELLED_CODE = 202
ACK_TIMEOUT_S = 5.0
WORKING = ("SENT", "WORKING")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderTicket:
    """An order as §15.4 defines it, in IB's own field values (order_type MKT or LMT, tif OPG or DAY)."""

    con_id: int
    symbol: str
    exchange: str
    currency: str
    action: str
    quantity: float
    order_type: str
    limit_price: float | None
    tif: str
    outside_rth: bool
    account: str
    order_ref: str


@dataclass(frozen=True)
class OrderReport:
    """An order as IB reports it: state is WORKING, FILLED, CANCELLED or REJECTED, with IB's last error code."""

    order_id: int
    order_ref: str
    con_id: int
    state: str
    filled: float
    avg_price: float
    commission: float
    error_code: int | None = None


class OrderPort(Protocol):
    def place(self, ticket: OrderTicket) -> int: ...

    def cancel(self, order_id: int) -> bool: ...

    def open_orders(self) -> list[OrderReport]: ...

    async def completed_orders(self) -> list[OrderReport]: ...

    def on_report(self, handler: Callable[[OrderReport], None]) -> None: ...


class AccountPort(Protocol):
    """What trade mode reads of IB besides orders: the connection, the account, its positions, contracts and price
    rules. ib_link.IbLink is the real one."""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    def account(self) -> str | None: ...

    def positions(self) -> dict[int, float]: ...

    async def contract(self, market: str, symbol: str, exchange: str | None) -> Contract: ...

    async def increments(self, rule_id: int) -> list[tuple[float, float]] | None: ...


class TradeError(Exception):
    """A batch trade mode refuses to send; code is the API's (§19): NOT_CONNECTED, BATCH_EXPIRED or BATCH_STALE."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


Guard = Callable[[], AbstractAsyncContextManager[IbguardClient]]


class Trader:
    """Trade mode's orders. ibguard opens a logged-in ibguard client for one task, or is None without ibguard."""

    def __init__(self, store: Store, orders: OrderPort, account: AccountPort, limits: Limits, *, ibguard: Guard | None,
                 notifier: Notifier, clock: Callable[[], float], mode: str = "trade") -> None:
        self.store, self.orders, self.account, self.limits = store, orders, account, limits
        self.ibguard, self.notifier, self.clock, self.mode = ibguard, notifier, clock, mode
        self._acknowledged: set[int] = set()
        self._stopped: set[int] = set()
        self._unrestored: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        orders.on_report(self.on_report)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def event(self, level: str, kind: str, message: str, market: str | None = None, symbol: str | None = None) -> None:
        self.store.append_event(self.now(), level, kind, message, market, symbol)

    # ----- a batch from IB's positions (§14.2, §15.2, §15.3) -----

    async def lines(self, portfolio: LivePortfolio, weights: Mapping[str, float], closes: Mapping[str, float | None],
                    session_day: int, us_order_kind: str, offset_pct: float) -> tuple[list[Line], dict[str, float]]:
        """The lines of a batch and the holdings they were built on: IB's quantities, the ledger cash plus their value
        as the live equity, the contracts' lots, sizes and price rules, then the limits."""
        market = portfolio.market
        await self.account.connect()
        positions = self.account.positions()
        contracts = {symbol: await self._contract(market, symbol) for symbol in weights}
        current = {s: positions.get(c.con_id, 0.0) for s, c in contracts.items() if c is not None and positions.get(c.con_id)}
        ledger = self.store.ledger(portfolio.id)
        cash = ledger[-1].cash if ledger else portfolio.capital
        equity = cash + sum(quantity * closes[symbol] for symbol, quantity in current.items())
        lots = {s: live_lot(market, c.size_increment if c else None) for s, c in contracts.items()}
        lines = batch_lines(market, weights, closes, current, equity, lots, session_day, us_order_kind, offset_pct)
        lines = [await self._tradeable(line, contracts.get(line.symbol)) for line in lines]
        return apply_limits(lines, equity, self.limits, market), current

    async def _contract(self, market: str, symbol: str) -> Contract | None:
        """The cached contract, else IB's unique match, cached (§15.2); None when IB has no unique one."""
        found = self.store.contract(market, symbol)
        if found is not None:
            return found
        exchange = next((s.exchange for s in self.store.symbols(market) if s.symbol == symbol), None)
        try:
            found = await self.account.contract(market, symbol, exchange)
        except Exception as error:  # an IbError of any kind: no unique contract to trade
            log.warning("no contract for %s %s: %s", market, symbol, error)
            return None
        self.store.save_contract(market, symbol, found)
        return found

    async def _tradeable(self, line: Line, contract: Contract | None) -> Line:
        """A proposed line on its contract: SKIPPED_NO_CONTRACT, SKIPPED_MIN_SIZE, or its limit on the price rule's
        grid (SKIPPED_NO_PRICE_RULE without one)."""
        if line.status != "PROPOSED":
            return replace(line, con_id=contract and contract.con_id)
        if contract is None:
            return replace(line, status="SKIPPED_NO_CONTRACT", reason="IB has no unique contract")
        line = replace(line, con_id=contract.con_id)
        if contract.min_size and line.quantity < contract.min_size:
            return replace(line, status="SKIPPED_MIN_SIZE", reason=f"below IB's minimum size {contract.min_size:g}")
        if line.kind != "LIMIT":
            return line
        rule = contract.market_rule_ids.split(",")[0] if contract.market_rule_ids else ""
        increments = await self.account.increments(int(rule)) if rule.isdigit() else None
        if not increments:
            return replace(line, status="SKIPPED_NO_PRICE_RULE", reason="IB gave no price rule for the contract")
        return replace(line, limit_price=rule_price(line.limit_price, increments, line.action))

    # ----- sending (§15.5, §16.2) -----

    def precheck(self, batch: LiveBatch, market: str) -> None:
        """Raises TradeError unless the link is connected on the right account, the cutoff is ahead and IB's positions
        are still those the batch was built on."""
        if not self.account.connected or self.account.account() is None:
            raise TradeError("NOT_CONNECTED", "IB is not connected on the configured account")
        if self.now() >= cutoff(market, batch.session_day):
            raise TradeError("BATCH_EXPIRED", f"batch {batch.id}'s cutoff has passed")
        positions = self.account.positions()
        seen = {line.symbol: line.con_id for line in batch.lines if line.con_id is not None}
        now = {symbol: positions.get(con_id, 0.0) for symbol, con_id in seen.items() if positions.get(con_id)}
        if now != {symbol: quantity for symbol, quantity in batch.positions_seen.items() if quantity}:
            raise TradeError("BATCH_STALE", "IB's positions changed since the batch was built: regenerate it")

    async def send(self, batch: LiveBatch, portfolio: LivePortfolio) -> tuple[str, str]:
        """LIVE_SEND: the confirmed lines, reducing ones first, each through ibguard's protocol when it reduces a
        position, then the send function. A read-only API stops the batch (§15.9)."""
        market = portfolio.market
        try:
            self.precheck(batch, market)
        except TradeError as refused:
            return "FAILED", f"{refused.code}: {refused}"
        positions = self.account.positions()
        lines = self._within_limits(batch, portfolio)
        lines.sort(key=lambda line: not _reducing(line, positions))
        counts = {"sent": 0, "skipped": 0}
        async with self._guard_if(any(_reducing(line, positions) for line in lines)) as guard:
            for line in lines:
                if batch.id in self._stopped:
                    self.store.update_order(line.id, self.now(), status="NOT_SENT", reason="the batch stopped")
                    continue
                skip = await self._protect(guard, line, positions) if _reducing(line, positions) else None
                skip = skip or self._refusal(line, positions)
                if skip is not None:
                    self.store.update_order(line.id, self.now(), status=skip[0], reason=skip[1])
                    counts["skipped"] += 1
                    continue
                await self._place(batch, line, market)
                counts["sent"] += 1
        self.store.set_batch_status(batch.id, "SENT")
        self._finish(batch.id)
        return "DONE", f"batch {batch.id}: sent {counts['sent']}, skipped {counts['skipped']}"

    def _within_limits(self, batch: LiveBatch, portfolio: LivePortfolio) -> list[LiveOrder]:
        """The limits again, just before sending (§15.3); the confirmed lines that pass."""
        confirmed = [line for line in batch.lines if line.status == "CONFIRMED"]
        ledger = self.store.ledger(portfolio.id)
        equity = ledger[-1].equity if ledger else portfolio.capital
        checked = apply_limits([Line(o.symbol, o.action, o.quantity, o.kind, o.limit_price, o.ref_price, "PROPOSED")
                                for o in confirmed], equity, self.limits, portfolio.market)
        passed = []
        for order, line in zip(confirmed, checked):
            if line.status == "BLOCKED_LIMIT":
                self.store.update_order(order.id, self.now(), status="BLOCKED_LIMIT", reason=line.reason)
            else:
                passed.append(order)
        return passed

    def _guard_if(self, needed: bool) -> AbstractAsyncContextManager[IbguardClient | None]:
        return _guard(self.ibguard if needed else None)

    async def _protect(self, guard: "IbguardClient | None | SourceError", line: LiveOrder,
                       positions: Mapping[int, float]) -> tuple[str, str] | None:
        """§16.2 for a reducing line: a skip, or None to send (after excluding the position when ibguard protects
        it and the owner has not excluded it)."""
        if self.ibguard is None:
            return None
        if isinstance(guard, SourceError):
            return "SKIPPED_IBGUARD_UNREACHABLE", str(guard)
        try:
            position = (await guard.state()).position(line.con_id)
            if position is None or position.status == "EXCLUDED":
                return None
            if position.status in EXITING or position.pending_exit:
                return "SKIPPED_IBGUARD_EXITING", f"ibguard is exiting the position ({position.status})"
            await guard.exclude(line.con_id, True)
            after = (await guard.state()).position(line.con_id)
        except SourceError as error:
            return "SKIPPED_IBGUARD_UNREACHABLE", str(error)
        if after is None or after.status != "EXCLUDED":
            return "SKIPPED_IBGUARD_EXCLUDE_FAILED", "ibguard did not show the position excluded"
        self.store.add_exclusion(line.id, line.con_id, line.symbol, self.now())
        self.event("INFO", "EXCLUDED", f"ibguard's protection lifted for btlab's {line.action} of {line.quantity:g}",
                   symbol=line.symbol)
        return None

    def _refusal(self, line: LiveOrder, positions: Mapping[int, float]) -> tuple[str, str] | None:
        """The one send function's refusals (§15.5)."""
        held = positions.get(line.con_id, 0.0)
        if self.mode != "trade":
            return "REFUSED", "the run mode is not trade"
        if not line.quantity > 0:
            return "REFUSED", "the quantity is not above 0"
        if _reducing(line, positions) and line.quantity > abs(held):
            return "REFUSED", f"it would trade more than the position of {abs(held):g}"
        if any(other.con_id == line.con_id and other.id != line.id for other in self.store.orders_with_status(*WORKING)):
            return "REFUSED", "a btlab order already works on the contract"
        return None

    async def _place(self, batch: LiveBatch, line: LiveOrder, market: str) -> None:
        """The ticket of §15.4, its orderRef stored with the line before it goes."""
        contract = self.store.contract(market, line.symbol)
        order_ref = f"{PREFIX}{batch.id}:{line.line}:{int(self.clock() * 1000)}"
        self.store.update_order(line.id, self.now(), order_ref=order_ref, status="SENT")
        moo = line.kind == "MOO"
        ticket = OrderTicket(line.con_id, line.symbol, contract.exchange or "SMART", contract.currency, line.action,
                             line.quantity, "MKT" if moo else "LMT", None if moo else line.limit_price, "OPG" if moo else "DAY",
                             False, self.account.account(), order_ref)
        order_id = self.orders.place(ticket)
        self.store.update_order(line.id, self.now(), ib_order_id=order_id)
        self.event("INFO", "ORDER_SENT", f"{line.action} {line.quantity:g} {line.kind} ({order_ref})", market, line.symbol)
        await self._wait_for(order_id)

    async def _wait_for(self, order_id: int) -> None:
        """IB's first answer to an order, so a read-only refusal stops the batch before its next order (§15.9)."""
        waited = 0.0
        while order_id not in self._acknowledged and waited < ACK_TIMEOUT_S:
            await asyncio.sleep(0.05)
            waited += 0.05

    # ----- following orders (§15.5 to §15.9) -----

    def on_report(self, report: OrderReport) -> None:
        """An order's new state from IB; orders without btlab's prefix are never btlab's concern (§15.8)."""
        if not report.order_ref.startswith(PREFIX):
            return
        self._acknowledged.add(report.order_id)
        line = self.store.order_by_ref(report.order_ref)
        if line is not None:
            self._apply(line, report)

    def _apply(self, line: LiveOrder, report: OrderReport, expire: bool = False) -> None:
        """A report on a line: its state and fill. An ended line keeps its end; a commission reported after the fill
        still reaches it."""
        state = report.state
        if state == "CANCELLED":
            state = "PARTIAL" if report.filled > 0 else ("EXPIRED" if expire else "CANCELLED")
        if line.status in ENDED and line.status != state:
            return
        if (line.status, line.filled_qty, line.commission or 0.0) == (state, report.filled, report.commission):
            return
        self.store.update_order(line.id, self.now(), status=state, ib_order_id=report.order_id, filled_qty=report.filled,
                                avg_price=report.avg_price if report.filled else None, commission=report.commission)
        if line.status == state:
            return
        if state == "REJECTED" and report.error_code == READ_ONLY_CODE:
            self._read_only(line)
        if state in ENDED:
            self.event("INFO", "ORDER_ENDED", f"{line.action} {line.symbol}: {state}, filled {report.filled:g}",
                       symbol=line.symbol)
            self._later(self.resolve_exclusions())
            self._finish(line.batch_id)

    def _read_only(self, line: LiveOrder) -> None:
        """IB's API is read-only: the batch stops, and the owner hears of it (§15.9)."""
        self._stopped.add(line.batch_id)
        message = f"IB refused batch {line.batch_id}'s order: the API is read-only; the batch stopped"
        self.event("ERROR", "READ_ONLY_API", message, symbol=line.symbol)
        for order in self.store.batch(line.batch_id).lines:
            if order.status == "CONFIRMED":
                self.store.update_order(order.id, self.now(), status="NOT_SENT", reason="the batch stopped: read-only API")
        self._later(self.notifier.send("READ_ONLY_API", batch=str(line.batch_id)))

    def _finish(self, batch_id: int) -> None:
        """A sent batch whose lines all ended is DONE, and the owner hears of its fills."""
        batch = self.store.batch(batch_id)
        if batch.status != "SENT" or any(line.status in (*WORKING, "CONFIRMED") for line in batch.lines):
            return
        self.store.set_batch_status(batch_id, "DONE")
        fills = "; ".join(f"{line.action} {line.filled_qty:g} {line.symbol} @ {line.avg_price:g}"
                          for line in batch.lines if line.filled_qty)
        self._later(self.notifier.send("BATCH_DONE", batch=str(batch_id), session=to_date(batch.session_day).isoformat(),
                                       fills=fills or "none"))

    def _later(self, work: Awaitable) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def recover(self) -> None:
        """At every connect: btlab's orders matched to their lines by exact orderRef (§15.7); one that matches none
        is recorded and left alone."""
        for report in [*self.orders.open_orders(), *await self.orders.completed_orders()]:
            if not report.order_ref.startswith(PREFIX):
                continue
            line = self.store.order_by_ref(report.order_ref)
            if line is None:
                self.event("WARNING", "UNKNOWN_BTLAB_ORDER", f"{report.order_ref} matches no line; left alone")
            else:
                self._apply(line, report)

    async def cancel(self, batch: LiveBatch) -> tuple[str, str]:
        """LIVE_CANCEL: btlab's own working orders of the batch (§15.8); their reports end the lines."""
        cancelled = sum(self.orders.cancel(line.ib_order_id) for line in batch.lines
                        if line.status in WORKING and line.ib_order_id is not None)
        return "DONE", f"batch {batch.id}: {cancelled} orders cancelled"

    async def sweep(self, market: str | None) -> tuple[str, str]:
        """LIVE_SWEEP, 15 minutes after a session, at start and at each connect: lines that have not ended are
        checked against IB's orders, a day order cancelled at the close EXPIRED; then ACTIVE exclusions (§15.6, §16.2)."""
        reports = {report.order_ref: report for report in [*self.orders.open_orders(), *await self.orders.completed_orders()]}
        checked = 0
        for line in self.store.orders_with_status(*WORKING):
            batch = self.store.batch(line.batch_id)
            portfolio = self.store.portfolio(batch.portfolio_id)
            if market is not None and portfolio.market != market:
                continue
            report = reports.get(line.order_ref)
            if report is None:
                report = OrderReport(line.ib_order_id or 0, line.order_ref or "", line.con_id or 0, "CANCELLED",
                                     line.filled_qty, line.avg_price or 0.0, line.commission or 0.0)
            if report.state != "WORKING":
                self._apply(line, report, expire=True)
                checked += 1
        await self.resolve_exclusions()
        return "DONE", f"lines ended {checked}, exclusions active {len(self.store.exclusions())}"

    def unresolved(self) -> bool:
        """An ACTIVE exclusion whose order has ended: the sweep tries again every minute."""
        return bool(self._unrestored) or any(self._ended(exclusion) for exclusion in self.store.exclusions())

    def _ended(self, exclusion: Exclusion) -> bool:
        line = self.store.order(exclusion.order_id)
        return line is not None and line.status in ENDED

    async def resolve_exclusions(self) -> None:
        """§16.2 step 6 for every ACTIVE exclusion whose order no longer works: included again while a position is
        left (RESTORED, with an email), or CLOSED. A failing include stays ACTIVE, with one event and one email."""
        pending = [exclusion for exclusion in self.store.exclusions() if self._ended(exclusion)]
        if not pending:
            return
        async with _guard(self.ibguard) as guard:
            for exclusion in pending:
                await self._resolve(guard, exclusion)

    async def _resolve(self, guard: "IbguardClient | None | SourceError", exclusion: Exclusion) -> None:
        line = self.store.order(exclusion.order_id)
        if not self.account.positions().get(exclusion.con_id):
            self.store.resolve_exclusion(exclusion.id, "CLOSED", self.now())
            return
        try:
            if isinstance(guard, SourceError):
                raise guard
            await guard.exclude(exclusion.con_id, False)
        except SourceError as error:
            if exclusion.id not in self._unrestored:
                self._unrestored.add(exclusion.id)
                self.event("ERROR", "IBGUARD_RESTORE_FAILED", f"ibguard's protection is still lifted: {error}",
                           symbol=exclusion.symbol)
                await self.notifier.send("IBGUARD_RESTORE_FAILED", symbol=exclusion.symbol, error=str(error))
            return
        self._unrestored.discard(exclusion.id)
        self.store.resolve_exclusion(exclusion.id, "RESTORED", self.now())
        filled = f"{line.filled_qty:g}" if line is not None else "0"
        self.event("INFO", "PROTECTION_RESTORED", f"ibguard protects the position again after btlab's order filled {filled}",
                   symbol=exclusion.symbol)
        await self.notifier.send("PROTECTION_RESTORED", symbol=exclusion.symbol, filled=filled)

    # ----- the ledger (§14.6) -----

    def catch_up(self, portfolio: LivePortfolio, aligned: Any, withholding_pct: float) -> None:
        """A ledger row per stored session since the last one: cash from fills, costs and dividends net of
        withholding; the holdings the ledger expects, and IB's quantities on the latest, with HOLDINGS_MISMATCH when
        they differ."""
        ledger = self.store.ledger(portfolio.id)
        cash = ledger[-1].cash if ledger else portfolio.capital
        expected = dict(ledger[-1].holdings) if ledger else {}
        rows = np.flatnonzero(aligned.days >= (ledger[-1].day + 1 if ledger else portfolio.first_day)).tolist()
        fills = self._fills(portfolio.id)
        for row in rows:
            day = int(aligned.days[row])
            for j, symbol in enumerate(aligned.symbols):
                quantity = expected.get(symbol, 0.0)
                if quantity and float(aligned.cash[row, j]):
                    dividend = float(aligned.cash[row, j])
                    cash += quantity * dividend * ((1 - withholding_pct / 100) if quantity > 0 else 1)
                if quantity and float(aligned.ratio[row, j]) != 1.0:
                    expected[symbol] = quantity * float(aligned.ratio[row, j])
            for line in fills.get(day, ()):
                signed = line.filled_qty if line.action == "BUY" else -line.filled_qty
                cash -= signed * line.avg_price + (line.commission or 0.0)
                expected[line.symbol] = expected.get(line.symbol, 0.0) + signed
            expected = {symbol: quantity for symbol, quantity in expected.items() if quantity}
            holdings = self._ib_holdings(portfolio) if row == rows[-1] else expected
            if row == rows[-1] and holdings != expected:
                self.event("WARNING", "HOLDINGS_MISMATCH", f"IB holds {holdings}, the ledger expects {expected}",
                           portfolio.market)
            closes = _carried(aligned, row)
            equity = cash + sum(quantity * closes[symbol] for symbol, quantity in holdings.items() if symbol in closes)
            self.store.save_ledger(portfolio.id, LedgerRow(day, cash, equity, holdings))
            expected = dict(holdings)

    def _fills(self, portfolio_id: int) -> dict[int, list[LiveOrder]]:
        found: dict[int, list[LiveOrder]] = {}
        for batch in self.store.batches(portfolio_id, 10_000):
            for line in batch.lines:
                if line.filled_qty and line.avg_price is not None:
                    found.setdefault(batch.session_day, []).append(line)
        return found

    def _ib_holdings(self, portfolio: LivePortfolio) -> dict[str, float]:
        positions = self.account.positions()
        return {symbol: positions[contract.con_id] for symbol in portfolio.config["symbols"]
                if (contract := self.store.contract(portfolio.market, symbol)) and positions.get(contract.con_id)}


def _reducing(line: LiveOrder, positions: Mapping[int, float]) -> bool:
    """A sell of a long, or a buy covering a short."""
    held = positions.get(line.con_id, 0.0)
    return (line.action == "SELL" and held > 0) or (line.action == "BUY" and held < 0)


def _carried(aligned: Any, row: int) -> dict[str, float]:
    closes = {}
    for j, symbol in enumerate(aligned.symbols):
        known = np.flatnonzero(np.isfinite(aligned.close[:row + 1, j]))
        if len(known):
            closes[symbol] = float(aligned.close[known[-1], j])
    return closes


@asynccontextmanager
async def _guard(ibguard: Guard | None) -> AsyncIterator["IbguardClient | None | SourceError"]:
    """A logged-in ibguard client, None without ibguard, or the SourceError that kept it out of reach."""
    if ibguard is None:
        yield None
        return
    try:
        manager = ibguard()
        client = await manager.__aenter__()
    except SourceError as error:
        yield error
        return
    try:
        yield client
    finally:
        await manager.__aexit__(None, None, None)
