"""Fakes shared by the tests (SPEC.md §24.1): an HTTP session, an injected clock with its sleep, and a synchronous
stand-in for the process pool."""

import json
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Call:
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    timeout: Any


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
        self._body = answer.raw()

    async def read(self) -> bytes:
        return self._body


class _Exchange:
    """What session.get returns: the request happens when the context is entered, as in aiohttp."""

    def __init__(self, outcome: Answer | BaseException) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return FakeResponse(self._outcome)

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class FakeSession:
    """Answers GET requests from a script, in order, and records every call. It opens no connection."""

    def __init__(self, *outcomes: Answer | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Call] = []

    def get(self, url: str, *, params=None, headers=None, timeout=None) -> _Exchange:
        self.calls.append(Call(url, dict(params or {}), dict(headers or {}), timeout))
        if not self.outcomes:
            raise AssertionError(f"unexpected request number {len(self.calls)}")
        return _Exchange(self.outcomes.pop(0))

    async def __aenter__(self) -> "FakeSession":
        """A job opens its session as `async with session_factory() as session` (§5.7)."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


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
