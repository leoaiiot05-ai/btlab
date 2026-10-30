"""Interactive Brokers through ib_async (SPEC.md §5.4, §15.1): btlab's one connection, contracts and 1-minute history.

This module and ib_orders.py are the only ones that import ib_async; every name used here was checked against the
installed 2.1.0. Jobs reach IB through an IbLink, whose HistoryPort is ib_async's IB in production and a fake in tests.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol

import numpy as np
from ib_async import IB, RequestError, Stock

from btlab.calendar import session
from btlab.config import IBGUARD_CLIENT_ID, IbConfig
from btlab.records import Contract, MinuteBars, to_date

CONNECT_TIMEOUT_S = 30.0
REQUEST_TIMEOUT_S = 60.0
REQUEST_SPACING_S = 2.0
PACING_WAIT_S = 60.0
CURRENCIES = {"US": "USD", "TW": "TWD"}
# Missing market-data permissions or subscriptions, and an unknown contract (§5.4).
PERMISSION_CODES = frozenset({200, 354, 10089, 10090, 10091, 10168})
# IB answers a historical request without data, or over its pace, with this code and a message saying which.
HISTORY_CODE = 162
IB_KINDS = ("UNREACHABLE", "NO_DATA", "NO_PERMISSION", "PACING", "FAILED")


class IbError(Exception):
    """A request IB did not serve; kind is one of IB_KINDS. The text names the request, never an account."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class HistoryPort(Protocol):
    """The calls of ib_async's IB that history uses (§5.4); tests replace it with a fake."""

    async def connectAsync(self, host: str, port: int, clientId: int, timeout: float, readonly: bool,
                           account: str) -> Any: ...

    def isConnected(self) -> bool: ...

    def disconnect(self) -> Any: ...

    def managedAccounts(self) -> list[str]: ...

    def reqContractDetailsAsync(self, contract: Any) -> Awaitable[list]: ...

    async def reqHistoricalDataAsync(self, contract: Any, endDateTime: datetime, durationStr: str, barSizeSetting: str,
                                     whatToShow: str, useRTH: bool, formatDate: int, timeout: float) -> list: ...


def ib_port() -> HistoryPort:
    """ib_async's IB, raising a RequestError for a failed request instead of answering with nothing."""
    ib = IB()
    ib.RaiseRequestErrors = True
    return ib


class IbLink:
    """btlab's one IB connection (§5.4): made on demand, read-only in test mode, with the configured client id.
    clock gives UTC epoch seconds; the 2 s between historical requests are kept on it."""

    def __init__(self, config: IbConfig, mode: str, port: HistoryPort | None = None, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if config.client_id == IBGUARD_CLIENT_ID:
            raise ValueError(f"client id {IBGUARD_CLIENT_ID} is ibguard's")
        self.config, self.readonly = config, mode == "test"
        self.port = port or ib_port()
        self.clock, self.sleep = clock, sleep
        self.account_kind: str | None = None
        self._last_request: float | None = None

    @property
    def connected(self) -> bool:
        return self.port.isConnected()

    async def connect(self) -> None:
        """Connects unless connected; UNREACHABLE when IB Gateway does not answer within 30 s."""
        if self.connected:
            return
        try:
            await self.port.connectAsync(self.config.host, self.config.port, clientId=self.config.client_id,
                                         timeout=CONNECT_TIMEOUT_S, readonly=self.readonly, account=self.config.account)
        except (OSError, TimeoutError) as error:
            raise IbError("UNREACHABLE", f"IB cannot be reached ({type(error).__name__})") from None
        self.account_kind = account_kind(self.config.account, self.port.managedAccounts())

    def close(self) -> None:
        self.port.disconnect()

    async def contract(self, market: str, symbol: str, exchange: str | None) -> Contract:
        """The symbol's IB contract, when IB has exactly one (§5.4): NO_PERMISSION for none, FAILED for several."""
        stock = _stock(market, symbol, exchange)
        details = await _answer(f"contract {symbol}", lambda: self.port.reqContractDetailsAsync(stock), REQUEST_TIMEOUT_S)
        if not details:
            raise IbError("NO_PERMISSION", f"IB knows no contract for {symbol}")
        if len(details) > 1:
            raise IbError("FAILED", f"IB has {len(details)} contracts for {symbol}")
        found = details[0]
        return Contract(found.contract.conId, found.contract.exchange or None, found.contract.currency,
                        _size(found.minSize), _size(found.sizeIncrement), found.marketRuleIds or None,
                        datetime.fromtimestamp(self.clock(), timezone.utc))

    async def minute_bars(self, market: str, symbol: str, contract: Contract, day: int) -> MinuteBars:
        """The day's 1-minute bars of the regular session, split-adjusted as IB returns them: one request, paced, and
        once more after a pacing violation (§5.4)."""
        start, end = session(market, day)
        stock = _stock(market, symbol, contract.exchange, contract.con_id)
        label = f"bars {symbol} {to_date(day).isoformat()}"
        for retry in (False, True):
            await self._paced()
            try:
                bars = await _answer(label, lambda: self.port.reqHistoricalDataAsync(
                    stock, endDateTime=datetime.fromtimestamp(end, timezone.utc), durationStr="1 D",
                    barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, formatDate=2, timeout=REQUEST_TIMEOUT_S))
                break
            except IbError as error:
                if error.kind != "PACING" or retry:
                    raise
                await self.sleep(PACING_WAIT_S)
        if not bars:
            # ib_async answers with nothing when its own 60 s timeout passes; IB's "no data" is an error.
            raise IbError("FAILED", f"IB did not answer within {REQUEST_TIMEOUT_S:g} s ({label})")
        return _session_bars(bars, start, end, label)

    async def _paced(self) -> None:
        """At least 2 s between historical requests: IB soft-throttles 1-minute bars (§5.4)."""
        if self._last_request is not None:
            wait = self._last_request + REQUEST_SPACING_S - self.clock()
            if wait > 0:
                await self.sleep(wait)
        self._last_request = self.clock()


def account_kind(configured: str, accounts: list[str]) -> str | None:
    """paper for an account id starting with D, else live (§3); None while the account is not known."""
    account = configured or (accounts[0] if len(accounts) == 1 else "")
    if not account:
        return None
    return "paper" if account.startswith("D") else "live"


def _stock(market: str, symbol: str, exchange: str | None, con_id: int = 0) -> Stock:
    """US through SMART, with a class share's "." as " "; TW on the exchange recorded for the symbol (§5.4, §7)."""
    if market == "US":
        return Stock(symbol.replace(".", " "), "SMART", CURRENCIES[market], conId=con_id)
    if not exchange:
        raise IbError("FAILED", f"no exchange is recorded for {symbol}")
    return Stock(symbol, exchange, CURRENCIES[market], conId=con_id)


async def _answer(label: str, request: Callable[[], Awaitable], timeout: float | None = None) -> Any:
    """A request's answer, or the IbError its failure means. ib_async sends some requests before they are awaited,
    so request is called here. timeout is for requests without ib_async's own."""
    try:
        answer = request()
        return await (answer if timeout is None else asyncio.wait_for(answer, timeout))
    except RequestError as error:
        raise IbError(error_kind(error.code, error.message), f"IB error {error.code} ({label}): {error.message}") from None
    except TimeoutError:
        raise IbError("FAILED", f"IB did not answer within {timeout:g} s ({label})") from None
    except OSError as error:
        raise IbError("UNREACHABLE", f"the IB connection is lost ({type(error).__name__}, {label})") from None


def error_kind(code: int, message: str) -> str:
    text = message.lower()
    if code in PERMISSION_CODES or "permission" in text or "not subscribed" in text:
        return "NO_PERMISSION"
    if "pacing" in text:
        return "PACING"
    if code == HISTORY_CODE and "no data" in text:
        return "NO_DATA"
    return "FAILED"


def _session_bars(bars: list, start: int, end: int, label: str) -> MinuteBars:
    """IB's bars by their UTC start second (formatDate=2), inside the regular session only (§4.2)."""
    t = np.array([int(bar.date.timestamp()) for bar in bars], dtype=np.int64)
    inside = (t >= start) & (t < end)
    if not inside.any():
        raise IbError("NO_DATA", f"IB returned no bar of the regular session ({label})")
    columns = (np.array([getattr(bar, name) for bar in bars], dtype=np.float64)[inside]
               for name in ("open", "high", "low", "close", "volume"))
    return MinuteBars(t[inside], *columns)


def _size(value: float) -> float | None:
    """IB leaves an unknown size at 0, or at its unset maximum."""
    return float(value) if 0 < value < 1e300 else None
