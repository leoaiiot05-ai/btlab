"""Fakes shared by the tests (SPEC.md §24.1): an HTTP session, spylead's API, an injected clock with its sleep, and a
synchronous stand-in for the process pool."""

import json
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Any

from aiohttp import web


@dataclass
class Call:
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    timeout: Any
    method: str = "GET"
    json: Any = None


@dataclass
class Answer:
    """A scripted answer: a status with a body, given as JSON-able data or raw bytes, and headers."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def raw(self) -> bytes:
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


class FakeResponse:
    def __init__(self, answer: Answer) -> None:
        self.status = answer.status
        self.headers = answer.headers
        self.cookies = SimpleCookie(answer.headers.get("Set-Cookie", ""))
        self._body = answer.raw()

    async def read(self) -> bytes:
        return self._body


class _Exchange:
    """What session.request returns: the request happens when the context is entered, as in aiohttp."""

    def __init__(self, outcome: Answer | BaseException) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return FakeResponse(self._outcome)

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class FakeSession:
    """Answers requests from a script, in order, and records every call. It opens no connection."""

    def __init__(self, *outcomes: Answer | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Call] = []

    def request(self, method: str, url: str, *, params=None, json=None, headers=None, timeout=None) -> _Exchange:
        self.calls.append(Call(url, dict(params or {}), dict(headers or {}), timeout, method, json))
        if not self.outcomes:
            raise AssertionError(f"unexpected request number {len(self.calls)}")
        return _Exchange(self.outcomes.pop(0))

    async def __aenter__(self) -> "FakeSession":
        """A job opens its session as `async with session_factory() as session` (§5.7)."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class FakeSpylead:
    """spylead's documented JSON API, for an aiohttp test server on 127.0.0.1 (§24.1). The login takes the token and
    sets a session cookie, which every other route needs. Each request is recorded as (method, path, whether the
    cookie came). statuses are the series of the state; charts holds each chart's answer by path; failing paths
    answer 500."""

    COOKIE = ("spylead_session", "c0ffee")

    def __init__(self, token: str, statuses: dict[str, str] | None = None, f_status: str = "OK") -> None:
        self.token, self.f_status = token, f_status
        self.statuses = {"HYG": "KEPT", "XLF": "DROPPED", "TLT": "KEPT"} if statuses is None else statuses
        self.charts: dict[str, Any] = {
            "/api/charts/spy": {"rt": {"first_day": "2026-01-01", "values": [1.0, None, 3.0]}},
            "/api/charts/f": {"f": {"first_day": 20454, "values": [0.5, 0.25]},
                              "composite": {"first_day": 20454, "values": [None, 1.5]}},
            **{f"/api/charts/series/{name}": {"rt": {"first_day": "2026-01-02", "values": [2.0, -1.0]}} for name in self.statuses},
        }
        self.failing: set[str] = set()
        self.requests: list[tuple[str, str, bool]] = []

    def app(self) -> web.Application:
        @web.middleware
        async def guard(request: web.Request, handler: Callable) -> web.StreamResponse:
            signed_in = request.cookies.get(self.COOKIE[0]) == self.COOKIE[1]
            self.requests.append((request.method, request.path, signed_in))
            if request.path in self.failing:
                return web.json_response({"error": "INTERNAL"}, status=500)
            if request.path != "/api/login" and not signed_in:
                return web.json_response({"error": "UNAUTHORIZED"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[guard])
        app.router.add_post("/api/login", self._login)
        app.router.add_get("/api/state", self._state)
        app.router.add_get("/api/charts/{chart:.+}", self._chart)
        return app

    async def _login(self, request: web.Request) -> web.Response:
        if (await request.json()).get("token") != self.token:
            return web.json_response({"error": "LOGIN_FAILED"}, status=401)
        response = web.Response(status=204)
        response.set_cookie(*self.COOKIE, httponly=True)
        return response

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response({"series": [{"name": name, "status": status} for name, status in self.statuses.items()],
                                  "f": {"status": self.f_status}})

    async def _chart(self, request: web.Request) -> web.Response:
        if request.path not in self.charts:
            return web.json_response({"error": "NOT_FOUND"}, status=404)
        return web.json_response(self.charts[request.path])


class FakeIB:
    """ib_async's IB as ib_link's HistoryPort uses it (§24.1). It opens no connection: connectAsync records its
    arguments, contract details come from details (by symbol), and each historical request is recorded with the time on
    clock, then answered by history(contract, endDateTime): bars, or an exception to raise."""

    def __init__(self, clock: Callable[[], float], history: Callable[[Any, datetime], Any] | None = None,
                 accounts: tuple[str, ...] = ("DU1234567",)) -> None:
        self.clock, self.history, self.accounts = clock, history, list(accounts)
        self.refusal: BaseException | None = None
        self.up = False
        self.connects: list[dict] = []
        self.details: dict[str, Any] = {}
        self.asked: list[Any] = []
        self.requests: list[dict] = []

    async def connectAsync(self, host: str, port: int, clientId: int, timeout: float, readonly: bool, account: str) -> None:
        self.connects.append({"host": host, "port": port, "clientId": clientId, "timeout": timeout, "readonly": readonly,
                              "account": account})
        if self.refusal is not None:
            raise self.refusal
        self.up = True

    def isConnected(self) -> bool:
        return self.up

    def disconnect(self) -> None:
        self.up = False

    def managedAccounts(self) -> list[str]:
        return self.accounts

    def reqContractDetailsAsync(self, contract: Any):
        self.asked.append(contract)
        answer = self.details.get(contract.symbol, [])

        async def answered():
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return answered()

    async def reqHistoricalDataAsync(self, contract: Any, endDateTime: datetime, durationStr: str, barSizeSetting: str,
                                     whatToShow: str, useRTH: bool, formatDate: int, timeout: float) -> list:
        self.requests.append({"contract": contract, "endDateTime": endDateTime, "durationStr": durationStr,
                              "barSizeSetting": barSizeSetting, "whatToShow": whatToShow, "useRTH": useRTH,
                              "formatDate": formatDate, "timeout": timeout, "at": self.clock()})
        answer = self.history(contract, endDateTime)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeTime:
    """An epoch-seconds clock that only the injected sleep moves. Every sleep is recorded."""

    def __init__(self, start: datetime = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)) -> None:
        self.now = start.timestamp()
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _LaterFuture(Future):
    """A task that runs in this process when its result is first asked for, so a cancel leaves later tasks unrun."""

    def __init__(self, function: Callable, args: tuple) -> None:
        super().__init__()
        self._call = (function, args)

    def result(self, timeout: float | None = None) -> Any:
        if not self.done():
            function, args = self._call
            self.set_result(function(*args))
        return super().result(timeout)


class InlinePool:
    """The pool's in-process stand-in (§13.7): the initializer runs here once, then every task when its result is
    asked for. It counts the tasks it ran."""

    def __init__(self, initializer: Callable[..., None] | None = None, initargs: tuple = ()) -> None:
        if initializer is not None:
            initializer(*initargs)
        self.ran = 0

    def submit(self, function: Callable, *args: Any) -> Future:
        def counted(*call_args: Any) -> Any:
            self.ran += 1
            return function(*call_args)

        return _LaterFuture(counted, args)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None

    def __enter__(self) -> "InlinePool":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None
