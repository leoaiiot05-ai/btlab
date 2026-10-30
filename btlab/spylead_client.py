"""spylead, the owner's research tool, through its JSON API (SPEC.md §5.6): a login, its state, then the series btlab
keeps as external series. Never its database.

The session cookie lives in this object only. Like the token, it never reaches a log, an event or an error (§21).
"""

from datetime import date
from typing import Any
from urllib.parse import quote

import numpy as np

from btlab.http import Requester, SourceError, field, rows_of
from btlab.records import ExternalSeries, to_day

SOURCE = "spylead"


class SpyleadClient:
    def __init__(self, url: str, token: str, requester: Requester) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._requester = requester
        self._cookie = ""

    async def snapshot(self) -> tuple[ExternalSeries, ...]:
        """SPYL1, one LEADL1_<name> per series spylead keeps (status KEPT), and F and COMPOSITE, in-sample, when
        spylead's F is OK."""
        if not self._token:
            raise SourceError("MISSING_KEY", "the spylead token is empty")
        await self._login()
        state = await self._get("state", "/api/state")
        found = [series("SPYL1", field(await self._get("SPY", "/api/charts/spy"), "rt", "spylead's SPY chart"), False)]
        for name in kept(state):
            chart = await self._get(f"series {name}", f"/api/charts/series/{quote(name, safe='')}")
            found.append(series(f"LEADL1_{name}", field(chart, "rt", f"spylead's series {name}"), False))
        if f_ready(state):
            chart = await self._get("F", "/api/charts/f")
            found += [series("F", field(chart, "f", "spylead's F chart"), True),
                      series("COMPOSITE", field(chart, "composite", "spylead's F chart"), True)]
        return tuple(found)

    async def _login(self) -> None:
        reply = await self._requester.send("POST", "login", f"{self._url}/api/login", body={"token": self._token})
        self._cookie = "; ".join(f"{name}={value}" for name, value in reply.cookies.items())

    async def _get(self, label: str, path: str) -> Any:
        headers = {"Cookie": self._cookie} if self._cookie else None
        return await self._requester.get_json(label, f"{self._url}{path}", {}, headers=headers)


def kept(state: Any) -> list[str]:
    """The names of the series whose status is KEPT, in spylead's order."""
    names = []
    for index, row in enumerate(rows_of(field(state, "series", "spylead's state"), "spylead's series")):
        where = f"spylead's series, row {index + 1}"
        name = field(row, "name", where)
        if not isinstance(name, str) or not name:
            raise SourceError("BAD_PAYLOAD", f"{where}: name is not a text")
        if field(row, "status", where) == "KEPT":
            names.append(name)
    return names


def f_ready(state: Any) -> bool:
    return field(field(state, "f", "spylead's state"), "status", "spylead's f") == "OK"


def series(name: str, payload: Any, in_sample: bool) -> ExternalSeries:
    """{first_day, values} over calendar days: first_day a YYYY-MM-DD date or a day number, null values NaN."""
    where = f"spylead's {name}"
    values = field(payload, "values", where)
    if not isinstance(values, list) or not all(_value(value) for value in values):
        raise SourceError("BAD_PAYLOAD", f"{where}: values is not a list of numbers")
    numbers = np.array([np.nan if value is None else value for value in values], dtype=np.float64)
    return ExternalSeries(name, _first_day(field(payload, "first_day", where), where), numbers, in_sample)


def _value(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _first_day(value: Any, where: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return to_day(date.fromisoformat(value))
    except (TypeError, ValueError):
        raise SourceError("BAD_PAYLOAD", f"{where}: first_day is not a day") from None
