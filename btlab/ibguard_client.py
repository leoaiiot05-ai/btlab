"""ibguard, the owner's exit engine, through its JSON API (SPEC.md §16): its state, its STK thresholds, and lifting or
restoring its protection of a position around btlab's sells.

The session cookie lives in this object only. Like the token, it never reaches a log, an event or an error (§21).
"""

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from btlab.config import ServiceConfig
from btlab.http import Requester, SourceError, field, rows_of

SOURCE = "ibguard"
# ibguard is exiting the position, or cannot confirm it: btlab must not race it (§16.2 step 3).
EXITING = ("EXITING", "EXIT_PENDING", "POSITION_UNCONFIRMED")
TP_MAX, SL_MAX = 1000.0, 100.0


@dataclass(frozen=True)
class GuardedPosition:
    """One of ibguard's REAL positions; ref and price are None when ibguard has none."""

    con_id: int
    symbol: str
    sec_type: str
    quantity: float
    ref: float | None
    price: float | None
    status: str
    pending_exit: bool


@dataclass(frozen=True)
class GuardState:
    account: str
    account_kind: str | None
    paused: bool
    thresholds: dict
    positions: tuple[GuardedPosition, ...]

    def position(self, con_id: int) -> GuardedPosition | None:
        return next((found for found in self.positions if found.con_id == con_id), None)


class IbguardClient:
    def __init__(self, url: str, token: str, requester: Requester) -> None:
        self._url, self._token, self._requester = url.rstrip("/"), token, requester
        self._cookie = ""

    async def login(self) -> None:
        if not self._token:
            raise SourceError("MISSING_KEY", "the ibguard token is empty")
        reply = await self._requester.send("POST", "login", f"{self._url}/api/login", body={"token": self._token})
        self._cookie = "; ".join(f"{name}={value}" for name, value in reply.cookies.items())

    async def state(self) -> GuardState:
        return guard_state(await self._send("GET", "state", "/api/state"))

    async def write_thresholds(self, tp_pct: float, sl_pct: float) -> None:
        """The STK thresholds only; other security types stay as they are (§16.3)."""
        await self._send("POST", "thresholds", "/api/thresholds", {"STK": {"tp_pct": tp_pct, "sl_pct": sl_pct}})

    async def exclude(self, con_id: int, excluded: bool) -> None:
        label = f"{'exclude' if excluded else 'include'} {con_id}"
        await self._send("POST", label, f"/api/positions/REAL/{con_id}/exclude", {"excluded": excluded})

    async def _send(self, method: str, label: str, path: str, body: Any = None) -> Any:
        headers = {"Cookie": self._cookie} if self._cookie else None
        return (await self._requester.send(method, label, f"{self._url}{path}", body=body, headers=headers)).body


@asynccontextmanager
async def connected(config: ServiceConfig, session: Callable[[], Any], sleep: Callable[[float], Awaitable[None]],
                    clock: Callable[[], float]) -> AsyncIterator[IbguardClient]:
    """A logged-in client on an aiohttp session of its own, for one task (§5.7)."""
    async with session() as opened:
        client = IbguardClient(config.url, config.token, Requester(SOURCE, opened, sleep=sleep, clock=clock))
        await client.login()
        yield client


def guard_state(payload: Any) -> GuardState:
    """ibguard's state: its account, whether it is paused, its thresholds by security type, and its REAL positions."""
    where = "ibguard's state"
    positions = []
    for index, row in enumerate(rows_of(field(payload, "positions", where), "ibguard's positions")):
        if field(row, "source", f"ibguard's position {index + 1}") != "REAL":
            continue
        position = f"ibguard's position {index + 1}"
        positions.append(GuardedPosition(int(field(row, "con_id", position)), str(field(row, "symbol", position)),
                                         str(field(row, "sec_type", position)), float(field(row, "quantity", position)),
                                         _number(row.get("ref")), _number(row.get("price")), str(field(row, "status", position)),
                                         bool(row.get("pending_exit"))))
    thresholds = field(payload, "thresholds", where)
    if not isinstance(thresholds, dict):
        raise SourceError("BAD_PAYLOAD", f"{where}: thresholds is not an object")
    return GuardState(str(field(payload, "account", where)), payload.get("account_kind"), bool(field(payload, "paused", where)),
                      thresholds, tuple(positions))


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def thresholds_error(tp_pct: float, sl_pct: float) -> str | None:
    """ibguard's ranges: tp_pct in (0, 1000], sl_pct in (0, 100) (§16.3)."""
    if not 0 < tp_pct <= TP_MAX:
        return f"tp_pct must be above 0 and at most {TP_MAX:g}"
    if not 0 < sl_pct < SL_MAX:
        return f"sl_pct must be above 0 and below {SL_MAX:g}"
    return None


def preview(state: GuardState, tp_pct: float, sl_pct: float) -> dict:
    """What writing the STK thresholds would do (§16.3): ibguard's account and paused state, the current and the new
    thresholds, and every STK position that would exit at once, long and short, with those without a price listed as
    unknown. The token is the SHA-256 of the canonical JSON of the account, the new thresholds and that list."""
    new = {"tp_pct": tp_pct, "sl_pct": sl_pct}
    affected = [found for position in state.positions if position.sec_type == "STK"
                if (found := _exit(position, tp_pct / 100, sl_pct / 100)) is not None]
    signed = {"account": state.account, "new": new, "affected": affected}
    token = hashlib.sha256(json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"account": state.account, "account_kind": state.account_kind, "paused": state.paused,
            "current": state.thresholds.get("STK"), "new": new, "affected": affected, "token": token}


def _exit(position: GuardedPosition, tp: float, sl: float) -> dict | None:
    """The exit a position would take at once: for a long, price ≥ ref × (1 + tp) is TAKE_PROFIT and price ≤
    ref × (1 − sl) STOP_LOSS; shorts are mirrored."""
    found = {"symbol": position.symbol, "con_id": position.con_id, "side": "LONG" if position.quantity > 0 else "SHORT",
             "quantity": abs(position.quantity), "ref": position.ref, "price": position.price}
    if position.price is None or position.ref is None:
        return found | {"reason": "UNKNOWN"}
    price, ref = position.price, position.ref
    if position.quantity > 0:
        reason = "TAKE_PROFIT" if price >= ref * (1 + tp) else "STOP_LOSS" if price <= ref * (1 - sl) else None
    else:
        reason = "TAKE_PROFIT" if price <= ref * (1 - tp) else "STOP_LOSS" if price >= ref * (1 + sl) else None
    return None if reason is None else found | {"reason": reason}
