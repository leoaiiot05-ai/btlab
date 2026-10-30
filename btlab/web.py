"""The JSON API and the static UI on one aiohttp application (SPEC.md §19, §21).

The web layer validates input and calls the jobs and the store; it holds no backtest logic. Every answer carries the
headers of §21, and no answer, log line or error holds a secret.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from btlab.builtins import BUILTINS
from btlab.config import Config
from btlab.jobs import JobRunning
from btlab.markets import MARKETS, category_by_rule, normalize_symbol
from btlab.notifier import Notifier, Silent
from btlab.plugins import PluginFolder
from btlab.records import SymbolList, to_date
from btlab.registry import operands
from btlab.rules import validate_rule
from btlab.scheduler import next_time
from btlab.store import MinuteStore, Store, StoreError

HOST = "127.0.0.1"
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "btlab_session"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
LOGIN_DELAY_S = 1.0
EVENTS_SHOWN = 50
LANES = ("DATA", "MINUTE", "COMPUTE", "LIVE")
CANCELLABLE = ("DATA", "MINUTE", "COMPUTE")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

log = logging.getLogger(__name__)


class ApiError(Exception):
    """An answer in the error format of §19: {"error", "message", "path"?}, plus any extra fields."""

    def __init__(self, status: int, code: str, message: str, path: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.path, self.extra = status, code, path, extra

    def response(self) -> web.Response:
        body = {"error": self.code, "message": str(self), **({} if self.path is None else {"path": self.path}), **self.extra}
        return _json(body, self.status)


@dataclass(frozen=True, eq=False)
class Services:
    """What the web layer reaches. jobs is btlab.jobs.Jobs, or a stub in tests; clock gives UTC epoch seconds."""

    config: Config
    store: Store
    minute_store: MinuteStore
    jobs: Any
    plugins: PluginFolder
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    notifier: Notifier = Silent()


SERVICES = web.AppKey("services", Services)


def make_app(services: Services) -> web.Application:
    app = web.Application(middlewares=[_errors, _session])
    app[SERVICES] = services
    app.on_response_prepare.append(_headers)
    app.router.add_routes([
        web.post("/api/login", login),
        web.post("/api/logout", logout),
        web.get("/api/state", state),
        web.get("/api/lists", get_lists),
        web.post("/api/lists", create_list),
        web.put(r"/api/lists/{id:\d+}", update_list),
        web.delete(r"/api/lists/{id:\d+}", delete_list),
        web.get("/api/symbols", get_symbols),
        web.patch("/api/symbols/{market}/{symbol}", patch_symbol),
        web.delete("/api/symbols/{market}/{symbol}", delete_symbol),
        web.post("/api/update", update),
        web.post("/api/jobs/{lane}/cancel", cancel),
        web.get("/api/strategies", get_strategies),
        web.post("/api/strategies/reload", reload_strategies),
        web.get("/api/indicators", get_indicators),
        web.get("/api/rules", get_rules),
        web.post("/api/rules", create_rule),
        web.put(r"/api/rules/{id:\d+}", update_rule),
        web.delete(r"/api/rules/{id:\d+}", delete_rule),
        web.post("/api/rules/validate", check_rule),
        web.get("/{path:.*}", static),
    ])
    return app


async def start_site(app: web.Application, port: int) -> web.AppRunner:
    """Serves the app on 127.0.0.1 only (§19, §21); Tailscale Serve publishes it inside the tailnet."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, port).start()
    return runner


def session_value(token: str) -> str:
    """The session cookie's value: the hex HMAC-SHA256 of "btlab-session" keyed with the web token (§19)."""
    return hmac.new(token.encode(), b"btlab-session", hashlib.sha256).hexdigest()


# ----- middleware -----


@web.middleware
async def _errors(request: web.Request, handler: Callable) -> web.StreamResponse:
    """The error format of §19 on /api; an unexpected exception is logged, recorded and answered 500 (§23)."""
    try:
        return await handler(request)
    except ApiError as error:
        return error.response()
    except JobRunning as busy:
        return ApiError(409, "JOB_RUNNING", str(busy)).response()
    except web.HTTPException as error:
        if not request.path.startswith("/api/"):
            raise
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(error.status, f"HTTP_{error.status}")
        return ApiError(error.status, code, error.reason).response()
    except Exception as error:
        log.exception("%s %s failed", request.method, request.path)
        services = request.app[SERVICES]
        message = f"{request.method} {request.path}: {type(error).__name__}"
        services.store.append_event(_now(services), "ERROR", "INTERNAL_ERROR", message)
        await services.notifier.send("INTERNAL_ERROR", error=message)
        return ApiError(500, "INTERNAL_ERROR", "an unexpected error; the log has the details").response()


@web.middleware
async def _session(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Every /api route but login needs the session cookie, compared in constant time (§19)."""
    if request.path.startswith("/api/") and request.path != "/api/login":
        cookie = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(cookie.encode(), session_value(request.app[SERVICES].config.web.token).encode()):
            raise ApiError(401, "UNAUTHORIZED", "log in first")
    return await handler(request)


async def _headers(request: web.Request, response: web.StreamResponse) -> None:
    """§21's headers on every answer, errors and static files included."""
    response.headers.update(HEADERS)
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"


# ----- bodies and fields -----


async def read_json(request: web.Request) -> Any:
    """A POST, PUT or PATCH body: application/json (415), valid JSON (INVALID_JSON) with finite numbers only."""
    if request.content_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "the body must be application/json")
    try:
        body = json.loads(await request.text())
    except ValueError:
        raise ApiError(400, "INVALID_JSON", "the body is not valid JSON") from None
    path = _non_finite(body, "")
    if path is not None:
        raise ApiError(400, "INVALID_FIELD", "must be a finite number", path)
    return body


def _non_finite(value: Any, path: str) -> str | None:
    """The path of the first NaN or infinity, which Python's JSON parser accepts and JSON does not."""
    if isinstance(value, float) and not math.isfinite(value):
        return path
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        found = _non_finite(item, f"{path}[{key}]" if isinstance(key, int) else f"{path}.{key}" if path else key)
        if found is not None:
            return found
    return None


def _object(body: Any, keys: tuple[str, ...]) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_FIELD", "the body must be an object", "")
    for key in body:
        if key not in keys:
            raise ApiError(400, "INVALID_FIELD", "is not a known field", key)
    return body


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "INVALID_FIELD", "must be a non-empty string", key)
    return value.strip()


def _market(value: Any, path: str) -> str:
    if value not in MARKETS:
        raise ApiError(400, "INVALID_FIELD", "must be US or TW", path)
    return value


def _symbols(market: str, value: Any) -> list[str]:
    """Symbols by §7's rules, normalized, each once."""
    if not isinstance(value, list):
        raise ApiError(400, "INVALID_FIELD", "must be a list of symbols", "symbols")
    found: list[str] = []
    for index, text in enumerate(value):
        try:
            symbol = normalize_symbol(market, text if isinstance(text, str) else "")
        except ValueError:
            raise ApiError(400, "INVALID_FIELD", f"is not a {market} symbol", f"symbols[{index}]") from None
        if symbol in found:
            raise ApiError(400, "INVALID_FIELD", "appears twice", f"symbols[{index}]")
        found.append(symbol)
    return found


def _id(request: web.Request) -> int:
    return int(request.match_info["id"])


def _conflict(error: StoreError, message: str) -> ApiError:
    if not error.conflict:
        raise error
    return ApiError(409, "ALREADY_EXISTS", message)


# ----- answers -----


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=partial(json.dumps, allow_nan=False))


def _now(services: Services) -> datetime:
    return datetime.fromtimestamp(services.clock(), timezone.utc)


def _utc(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(day: int | None) -> str | None:
    return None if day is None else to_date(day).isoformat()


def _services(request: web.Request) -> Services:
    return request.app[SERVICES]


# ----- session -----


async def login(request: web.Request) -> web.Response:
    """204 and the session cookie; a wrong token waits 1 s, then 401 LOGIN_FAILED (§19)."""
    services = _services(request)
    token = _object(await read_json(request), ("token",)).get("token")
    expected = services.config.web.token
    if not isinstance(token, str) or not hmac.compare_digest(token.encode(), expected.encode()):
        await services.sleep(LOGIN_DELAY_S)
        raise ApiError(401, "LOGIN_FAILED", "the token is not the configured one")
    response = web.Response(status=204)
    response.set_cookie(COOKIE, session_value(expected), max_age=COOKIE_MAX_AGE_S, path="/", httponly=True,
                        samesite="Strict", secure=request.headers.get("X-Forwarded-Proto") == "https")
    return response


async def logout(request: web.Request) -> web.Response:
    await read_json(request)
    response = web.Response(status=204)
    response.del_cookie(COOKIE, path="/")
    return response


# ----- state -----


async def state(request: web.Request) -> web.Response:
    """The state document (§19): the app, the markets, the jobs and the latest events. services and live arrive with
    their features."""
    services = _services(request)
    now = _now(services)
    running = services.jobs.running()
    return _json({
        "app": _app_state(services.config, now),
        "markets": {market: _market_state(services.store, market) for market in MARKETS},
        "jobs": {lane: running.get(lane) for lane in LANES},
        "events": [
            {"id": e.id, "ts": _utc(e.ts), "level": e.level, "kind": e.kind, "market": e.market, "symbol": e.symbol,
             "message": e.message}
            for e in services.store.latest_events(EVENTS_SHOWN)
        ],
    })


def _app_state(config: Config, now: datetime) -> dict:
    """Whether each key is set, never the key (§19)."""
    schedule = config.schedule
    times = {"US": schedule.us_update, "TW": schedule.tw_update}
    return {
        "server_time": _utc(now),
        "mode": config.mode,
        "keys": {"tiingo": bool(config.tiingo.api_key), "finmind": bool(config.finmind.token), "fred": bool(config.fred.api_key)},
        "limits": None if config.mode == "test" or config.limits is None else asdict(config.limits),
        "schedule": {"enabled": schedule.enabled,
                     "next": {m: _utc(next_time(m, at, now)) for m, at in times.items()} if schedule.enabled else None},
    }


def _market_state(store: Store, market: str) -> dict:
    finished = next((run for run in store.latest_job_runs(f"UPDATE_{market}", 5) if run.finished_at is not None), None)
    return {
        "data_start": _iso(store.data_start(market)),
        "last_day": _iso(store.market_last_day(market)),
        "symbols": len(store.symbols(market)),
        "last_update": None if finished is None else
        {"outcome": finished.outcome, "finished_at": _utc(finished.finished_at), "message": finished.message},
    }


# ----- lists and symbols (§7) -----


def _list(found: SymbolList) -> dict:
    return {"id": found.id, "name": found.name, "market": found.market, "created_at": _utc(found.created_at),
            "symbols": list(found.symbols)}


def _find_list(store: Store, list_id: int) -> SymbolList:
    found = next((item for item in store.lists() if item.id == list_id), None)
    if found is None:
        raise ApiError(404, "NOT_FOUND", "no such list")
    return found


async def get_lists(request: web.Request) -> web.Response:
    return _json([_list(found) for found in _services(request).store.lists()])


async def create_list(request: web.Request) -> web.Response:
    services = _services(request)
    body = _object(await read_json(request), ("name", "market"))
    name, market = _text(body, "name"), _market(body.get("market"), "market")
    try:
        list_id = services.store.create_list(name, market, _now(services))
    except StoreError as error:
        raise _conflict(error, "a list already has that name") from None
    return _json({"id": list_id}, 201)


async def update_list(request: web.Request) -> web.Response:
    """A new symbol joins the market's symbols at once, with its category by rule (§7)."""
    services = _services(request)
    store, found = services.store, _find_list(services.store, _id(request))
    body = _object(await read_json(request), ("name", "symbols"))
    name = _text(body, "name") if "name" in body else None
    symbols = _symbols(found.market, body["symbols"]) if "symbols" in body else None
    if name is not None:
        try:
            store.rename_list(found.id, name)
        except StoreError as error:
            raise _conflict(error, "a list already has that name") from None
    if symbols is not None:
        for symbol in symbols:
            store.add_symbol(found.market, symbol, category_by_rule(found.market, symbol), _now(services))
        store.set_list_symbols(found.id, symbols)
    return web.Response(status=204)


async def delete_list(request: web.Request) -> web.Response:
    if not _services(request).store.delete_list(_id(request)):
        raise ApiError(404, "NOT_FOUND", "no such list")
    return web.Response(status=204)


async def get_symbols(request: web.Request) -> web.Response:
    """Each symbol's details, data range, last error, minute flag and minute coverage by status."""
    services = _services(request)
    market = _market(request.query.get("market"), "market")
    rows = []
    for info in services.store.symbols(market):
        found = services.store.daily_bar_range(market, info.symbol)
        coverage = Counter(day.status for day in services.minute_store.minute_coverage(market, info.symbol))
        rows.append({
            "symbol": info.symbol, "name": info.name, "category": info.category, "category_source": info.category_source,
            "exchange": info.exchange, "first_day": _iso(found and found[0]), "last_day": _iso(found and found[1]),
            "last_fetch_at": _utc(info.last_fetch_at), "last_error": info.last_fetch_error,
            "minute_enabled": info.minute_enabled, "borrow_fee_pct": info.borrow_fee_pct, "coverage": dict(coverage),
        })
    return _json(rows)


def _known_symbol(request: web.Request) -> tuple[str, str]:
    market, symbol = request.match_info["market"], request.match_info["symbol"]
    if market not in MARKETS or symbol not in {info.symbol for info in _services(request).store.symbols(market)}:
        raise ApiError(404, "NOT_FOUND", "no such symbol")
    return market, symbol


async def patch_symbol(request: web.Request) -> web.Response:
    """The owner's settings of a symbol (§7), each recorded as SETTING_CHANGED."""
    services = _services(request)
    store, (market, symbol) = services.store, _known_symbol(request)
    body = _object(await read_json(request), ("category", "minute_enabled", "borrow_fee_pct"))
    changes: list[tuple[Callable[[], object], str]] = []
    if "category" in body:
        category = body["category"]
        if category not in MARKETS[market].categories:
            raise ApiError(400, "INVALID_FIELD", "must be one of " + ", ".join(MARKETS[market].categories), "category")
        changes.append((partial(store.set_category, market, symbol, category), f"category {category}"))
    if "minute_enabled" in body:
        enabled = body["minute_enabled"]
        if not isinstance(enabled, bool):
            raise ApiError(400, "INVALID_FIELD", "must be true or false", "minute_enabled")
        changes.append((partial(store.set_minute_enabled, market, symbol, enabled), f"minute bars {'on' if enabled else 'off'}"))
    if "borrow_fee_pct" in body:
        fee = body["borrow_fee_pct"]
        if market != "US":
            raise ApiError(400, "INVALID_FIELD", "only US symbols have a borrow fee", "borrow_fee_pct")
        if fee is not None and (isinstance(fee, bool) or not isinstance(fee, (int, float)) or fee < 0):
            raise ApiError(400, "INVALID_FIELD", "must be a number of at least 0, or null", "borrow_fee_pct")
        changes.append((partial(store.set_borrow_fee, market, symbol, None if fee is None else float(fee)),
                        f"borrow fee {'default' if fee is None else f'{fee:g}%'}"))
    for apply, description in changes:
        apply()
        store.append_event(_now(services), "INFO", "SETTING_CHANGED", f"{symbol}: {description}", market, symbol)
    return web.Response(status=204)


async def delete_symbol(request: web.Request) -> web.Response:
    """Purges a symbol's data, minute bars first (§7); 409 IN_USE while a list holds it."""
    services = _services(request)
    market, symbol = _known_symbol(request)
    if any(symbol in found.symbols for found in services.store.lists(market)):
        raise ApiError(409, "IN_USE", "a list holds the symbol")
    services.minute_store.purge_symbol(market, symbol)
    services.store.purge_symbol(market, symbol)
    return web.Response(status=204)


# ----- jobs (§17) -----


async def update(request: web.Request) -> web.Response:
    """202 with the job's lane and kind; 409 JOB_RUNNING while the DATA lane is busy."""
    market = _market(_object(await read_json(request), ("market",)).get("market"), "market")
    job = _services(request).jobs.update(market)
    return _json({"lane": job.lane, "kind": job.kind}, 202)


async def cancel(request: web.Request) -> web.Response:
    """204, also when the lane is idle; the LIVE lane's jobs cannot be cancelled (§17.1)."""
    lane = request.match_info["lane"]
    if lane not in LANES:
        raise ApiError(404, "NOT_FOUND", "no such lane")
    await read_json(request)
    if lane not in CANCELLABLE:
        raise ApiError(409, "NOT_CANCELLABLE", f"the {lane} lane's jobs cannot be cancelled")
    _services(request).jobs.cancel(lane)
    return web.Response(status=204)


# ----- strategies, indicators and rules (§8.6, §9) -----


def _strategies(folder: PluginFolder) -> dict:
    """Built-ins with their schema per market, plugins with theirs, and the plugins that failed to load (§9.5)."""
    return {
        "builtins": [
            {"id": b.id, "name_en": b.name_en, "name_zh": b.name_zh, "output": b.output,
             "params": {market: {name: p.document() for name, p in b.schema(market, ()).items()} for market in MARKETS}}
            for b in BUILTINS.values()
        ],
        "plugins": [
            {"id": p.id, "name_en": p.name_en, "name_zh": p.name_zh, "timeframe": p.timeframe, "style": p.style,
             "output": p.output, "params": {name: param.document() for name, param in p.params.items()}}
            for p in folder.plugins.values()
        ],
        "errors": [{"id": error.id, "error": error.error} for error in folder.errors],
    }


async def get_strategies(request: web.Request) -> web.Response:
    return _json(_strategies(_services(request).plugins))


async def reload_strategies(request: web.Request) -> web.Response:
    await read_json(request)
    folder = _services(request).plugins
    folder.reload()
    return _json(_strategies(folder))


async def get_indicators(request: web.Request) -> web.Response:
    """The registry of §8.6."""
    return _json([
        {"name": o.name, "kind": o.kind, "outputs": list(o.outputs), "source": o.source, "timeframes": list(o.timeframes),
         "args": {name: {"type": a.type, "default": a.default, "min": a.min, "max": a.max} for name, a in o.args.items()}}
        for o in operands().values()
    ])


def _valid_rule(rule: Any) -> dict:
    issues = validate_rule(rule)
    if issues:
        raise ApiError(400, "RULE_INVALID", f"the rule has {len(issues)} errors", errors=_issues(issues))
    return rule


def _issues(issues: list) -> list[dict]:
    return [{"code": issue.code, "path": issue.path, "message": issue.message} for issue in issues]


async def get_rules(request: web.Request) -> web.Response:
    return _json([{"id": r.id, "name": r.name, "rule": r.rule, "updated_at": _utc(r.updated_at)}
                  for r in _services(request).store.rules()])


async def create_rule(request: web.Request) -> web.Response:
    services = _services(request)
    body = _object(await read_json(request), ("name", "rule"))
    name, rule = _text(body, "name"), _valid_rule(body.get("rule"))
    try:
        rule_id = services.store.create_rule(name, rule, _now(services))
    except StoreError as error:
        raise _conflict(error, "a rule already has that name") from None
    return _json({"id": rule_id}, 201)


async def update_rule(request: web.Request) -> web.Response:
    services = _services(request)
    body = _object(await read_json(request), ("name", "rule"))
    name, rule = _text(body, "name"), _valid_rule(body.get("rule"))
    try:
        found = services.store.update_rule(_id(request), name, rule, _now(services))
    except StoreError as error:
        raise _conflict(error, "a rule already has that name") from None
    if not found:
        raise ApiError(404, "NOT_FOUND", "no such rule")
    return web.Response(status=204)


async def delete_rule(request: web.Request) -> web.Response:
    if not _services(request).store.delete_rule(_id(request)):
        raise ApiError(404, "NOT_FOUND", "no such rule")
    return web.Response(status=204)


async def check_rule(request: web.Request) -> web.Response:
    issues = validate_rule(_object(await read_json(request), ("rule",)).get("rule"))
    return _json({"ok": not issues, "errors": _issues(issues)})


# ----- static files (§20) -----


async def static(request: web.Request) -> web.FileResponse:
    """The UI's files, without a session; / is index.html, and nothing outside the folder is served."""
    path = (STATIC / (request.match_info["path"] or "index.html")).resolve()
    if not path.is_relative_to(STATIC) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)
