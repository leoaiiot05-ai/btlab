"""Request rules shared by the HTTP clients: timeouts, retries, budgets and redaction (SPEC.md §5.7).

No log line and no error text holds a URL, a key or a token (§21): logs and errors describe a request by its label.
"""

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import aiohttp

from btlab.records import to_day

TIMEOUT_S = 30.0
RETRY_DELAYS_S = (2.0, 4.0, 8.0)
RETRY_AFTER_MAX_S = 60.0
# HTTP_<status> stands for one code per status.
SOURCE_ERROR_CODES = (
    "TIMEOUT", "HTTP_", "BAD_PAYLOAD", "MISSING_KEY", "SYMBOL_UNKNOWN", "RATE_LIMITED", "MAINTENANCE", "UNREACHABLE",
)

log = logging.getLogger(__name__)

_SECRET = re.compile(
    r"""(?ix)
    ( \b(?: api_?key | \w*token | authorization ) ['"]? \s* [:=] \s* ['"]?   # a secret's name and separator
      (?: (?: bearer | token ) \s+ )? )                                      # an authorization scheme
    [^\s'"&,;)}\]]+                                                          # the secret
    """
)
# A URL ends before whitespace, a bracket or a quote, and does not take a sentence's last punctuation.
_URL = re.compile(r"""\b[a-z][a-z0-9+.-]*://[^\s<>"'()\[\]{}]*[^\s<>"'()\[\]{}.,;:!?]""", re.I)


def redact(text: str) -> str:
    """Masks api_key, token and Authorization values (§5.7) in anything that may reach a log."""
    return _SECRET.sub(r"\1***", text)


class SourceError(Exception):
    """A data source failed. str() gives "<code>: <text>", the form stored in last_fetch_error (§18).

    The text never holds a URL, a key or a token, even when it quotes a service's own message.
    """

    def __init__(self, code: str, text: str) -> None:
        known = code[len("HTTP_"):].isdigit() if code.startswith("HTTP_") else code in SOURCE_ERROR_CODES
        if not known:
            raise ValueError(f"unknown source error code {code}")
        self.code = code
        self.text = redact(_URL.sub("<link>", text))
        super().__init__(f"{code}: {self.text}")


class RequestBudget:
    """At most count requests in any rolling window of seconds, for each (seconds, count) limit (§5.1, §5.2).

    times is the log of past requests in UTC epoch seconds. on_record receives the log after each request, so the
    caller can persist it and a restart does not reset the budget.
    """

    def __init__(
        self,
        limits: Sequence[tuple[float, int]],
        times: Iterable[float] = (),
        on_record: Callable[[list[float]], None] | None = None,
    ) -> None:
        self.limits = tuple(limits)
        self._horizon = max(seconds for seconds, _ in self.limits)
        self._times = sorted(float(moment) for moment in times)
        self._on_record = on_record

    @property
    def times(self) -> list[float]:
        return list(self._times)

    def wait(self, now: float) -> float:
        """Seconds until one more request fits every window; 0 when it fits now."""
        delay = 0.0
        for seconds, count in self.limits:
            recent = [moment for moment in self._times if moment > now - seconds]
            if len(recent) >= count:
                delay = max(delay, recent[-count] + seconds - now)
        return delay

    def record(self, now: float) -> None:
        self._times = [moment for moment in self._times if moment > now - self._horizon] + [now]
        if self._on_record is not None:
            self._on_record(self.times)


@dataclass(frozen=True)
class Reply:
    """A successful answer: its JSON body, None when it has none, and the cookies it set."""

    body: Any
    cookies: dict[str, str]


class Requester:
    """One source during one job: the session passed in, the 30 s timeout, retries, Retry-After and the budget."""

    def __init__(
        self,
        source: str,
        session: aiohttp.ClientSession,
        *,
        budget: RequestBudget | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        on_wait: Callable[[float], None] | None = None,
    ) -> None:
        self.source = source
        self.session = session
        self.budget = budget
        self.sleep = sleep
        self.clock = clock
        self.on_wait = on_wait

    async def get_json(
        self,
        label: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        error_text: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """The JSON body of a successful answer. label describes the request in logs and errors instead of the URL.

        error_text may take the text of an error from the JSON body of an unsuccessful answer.
        """
        return (await self.send("GET", label, url, params=params, headers=headers, error_text=error_text)).body

    async def send(
        self,
        method: str,
        label: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        error_text: Callable[[Any], str | None] | None = None,
    ) -> Reply:
        """One request, body sent as JSON, under the rules of §5.7. Any 2xx answer succeeds."""
        retries = 0
        throttled = False
        while True:
            await self._take_budget()
            log.debug("%s: %s", self.source, label)
            try:
                status, content, retry_after, cookies = await self._attempt(method, url, params, body, headers)
            except TimeoutError:
                failure = SourceError("TIMEOUT", f"{self.source} did not answer within {TIMEOUT_S:g} s: {label}")
            except aiohttp.ClientError as error:
                failure = SourceError("UNREACHABLE", f"{self.source} cannot be reached ({type(error).__name__}): {label}")
            else:
                if 200 <= status < 300:
                    text = f"{self.source} answered with a body that is not JSON: {label}"
                    return Reply(_parse(content, text) if content else None, cookies)
                if status == 429 and not throttled:
                    throttled = True
                    delay = _retry_after(retry_after)
                    log.warning("%s: rate limited; retrying in %g s: %s", self.source, delay, label)
                    await self.sleep(delay)
                    continue
                if status == 429:
                    raise SourceError("RATE_LIMITED", f"{self.source} is still rate limiting: {label}")
                failure = SourceError(f"HTTP_{status}", self._error_text(status, content, label, error_text))
                if status < 500:
                    raise failure
            if retries == len(RETRY_DELAYS_S):
                raise failure
            delay = RETRY_DELAYS_S[retries]
            retries += 1
            log.warning("%s: %s; retry %d in %g s", self.source, failure, retries, delay)
            await self.sleep(delay)

    async def _take_budget(self) -> None:
        if self.budget is None:
            return
        while (delay := self.budget.wait(self.clock())) > 0:
            until = self.clock() + delay
            log.info("%s: request budget spent; waiting until %s", self.source, _utc(until))
            if self.on_wait is not None:
                self.on_wait(until)
            await self.sleep(delay)
        self.budget.record(self.clock())

    async def _attempt(
        self, method: str, url: str, params: Mapping[str, str] | None, body: Any, headers: Mapping[str, str] | None
    ) -> tuple[int, bytes, str | None, dict[str, str]]:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
        async with self.session.request(method, url, params=params, json=body, headers=headers, timeout=timeout) as response:
            # The answer's own cookies: the session's jar refuses those of an IP host such as 127.0.0.1.
            cookies = {name: morsel.value for name, morsel in response.cookies.items()}
            return response.status, await response.read(), response.headers.get("Retry-After"), cookies

    def _error_text(
        self, status: int, body: bytes, label: str, error_text: Callable[[Any], str | None] | None
    ) -> str:
        text = None
        if error_text is not None:
            try:
                text = error_text(json.loads(body))
            except ValueError:
                pass
        return f"{self.source} answered {status}{f' ({text})' if text else ''}: {label}"


def fetch_start(data_start: int, last_stored_day: int | None, overlap_days: int) -> int:
    """The first day to request: data_start on the first fetch, else overlap_days before the latest stored day,
    because sources revise recent rows (§5.1, §5.2, §5.3). Never before data_start."""
    if last_stored_day is None:
        return data_start
    return max(data_start, last_stored_day - overlap_days)


def field(row: Any, key: str, where: str) -> Any:
    """row[key], or BAD_PAYLOAD naming where the row came from and the missing field."""
    if not isinstance(row, dict) or key not in row:
        raise SourceError("BAD_PAYLOAD", f"{where} has no field {key}")
    return row[key]


def number(row: Any, key: str, where: str) -> float:
    value = field(row, key, where)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SourceError("BAD_PAYLOAD", f"{where}: {key} is not a number")
    return float(value)


def day(row: Any, key: str, where: str) -> int:
    """A field holding YYYY-MM-DD, possibly followed by a time, as days since 1970-01-01."""
    value = field(row, key, where)
    try:
        return to_day(date.fromisoformat(value[:10]))
    except (TypeError, ValueError):
        raise SourceError("BAD_PAYLOAD", f"{where}: {key} is not a date") from None


def rows_of(payload: Any, where: str) -> list:
    if not isinstance(payload, list):
        raise SourceError("BAD_PAYLOAD", f"{where} is not a list")
    return payload


def _parse(body: bytes, text: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError("BAD_PAYLOAD", text) from None


def _retry_after(value: str | None) -> float:
    """Seconds from a Retry-After header, at most 60; 60 when it is absent or not a number (§5.7)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return RETRY_AFTER_MAX_S
    return min(max(seconds, 0.0), RETRY_AFTER_MAX_S) if math.isfinite(seconds) else RETRY_AFTER_MAX_S


def _utc(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
