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
from zoneinfo import ZoneInfo

from aiohttp import web

import numpy as np

from btlab.backtest import TIMEFRAMES, RunConfigError, equity_csv, stored_series, trades_csv
from btlab.builtins import BUILTINS
from btlab.calendar import session
from btlab.config import Config
from btlab.intraday import TIMEFRAME_MINUTES, resample
from btlab.jobs import JobRunning
from btlab.markets import MARKETS, category_by_rule, normalize_symbol
from btlab.metrics import drawdown
from btlab.notifier import Notifier, Silent
from btlab.optimize import NeedsConfirmation, OptimizeError, combination_config, heatmap
from btlab.plugins import PluginFolder
from btlab.prices import adjusted, align, day_events
from btlab.records import BacktestRow, ExternalSeries, MinuteBars, OptimizationRow, SymbolList, Trade, to_date, to_day
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
MAX_CANDLES = 5_000
# A minute candle window without "from" starts this long before "to".
MINUTE_WINDOW_S = 40 * 86_400
LIST_LIMIT, MAX_LIST_LIMIT = 100, 1_000
TRADES_LIMIT, MAX_TRADES_LIMIT = 500, 5_000
OFFLINE_RECENT = 20
SYNC_HEADER = "X-Btlab-Sync"
COMPARED = (2, 4)
KEY_METRICS = ("total_return", "cagr", "sharpe", "max_drawdown", "trades")
SERIES_SHOWN = ("EQUITY", "BENCHMARK", "CASH", "GROSS", "NET", "POSITIONS")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
# Named here because Windows' registry can map .js to text/plain, which browsers refuse for modules.
CONTENT_TYPES = {
    ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
    ".webmanifest": "application/manifest+json", ".png": "image/png",
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
        web.get("/api/spylead", get_spylead),
        web.post("/api/spylead/snapshot", snapshot),
        web.post("/api/jobs/{lane}/cancel", cancel),
        web.get("/api/strategies", get_strategies),
        web.post("/api/strategies/reload", reload_strategies),
        web.get("/api/indicators", get_indicators),
        web.get("/api/rules", get_rules),
        web.post("/api/rules", create_rule),
        web.put(r"/api/rules/{id:\d+}", update_rule),
        web.delete(r"/api/rules/{id:\d+}", delete_rule),
        web.post("/api/rules/validate", check_rule),
        web.post("/api/backtests", start_backtest),
        web.get("/api/backtests", list_backtests),
        web.get(r"/api/backtests/{id:\d+}", get_backtest),
        web.patch(r"/api/backtests/{id:\d+}", patch_backtest),
        web.delete(r"/api/backtests/{id:\d+}", delete_backtest),
        web.get(r"/api/backtests/{id:\d+}/series", get_series),
        web.get(r"/api/backtests/{id:\d+}/trades", get_trades),
        web.get(r"/api/backtests/{id:\d+}/candles/{symbol}", get_backtest_candles),
        web.get(r"/api/backtests/{id:\d+}/export/trades.csv", export_trades),
        web.get(r"/api/backtests/{id:\d+}/export/equity.csv", export_equity),
        web.get("/api/offline", get_offline),
        web.get("/api/compare", get_compare),
        web.get("/api/candles/{market}/{symbol}", get_candles),
        web.post("/api/optimizations", start_optimization),
        web.get("/api/optimizations", list_optimizations),
        web.get(r"/api/optimizations/{id:\d+}", get_optimization),
        web.get(r"/api/optimizations/{id:\d+}/heatmap", get_heatmap),
        web.post(r"/api/optimizations/{id:\d+}/promote", promote),
        web.delete(r"/api/optimizations/{id:\d+}", delete_optimization),
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
        code = HTTP_CODES.get(error.status, f"HTTP_{error.status}")
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
    """The state document (§19): the app with its services, the markets, the jobs and the latest events. IB, ibguard
    and live arrive with their features."""
    services = _services(request)
    now = _now(services)
    running = services.jobs.running()
    return _json({
        "app": _app_state(services, now),
        "markets": {market: _market_state(services.store, market) for market in MARKETS},
        "jobs": {lane: running.get(lane) for lane in LANES},
        "events": [
            {"id": e.id, "ts": _utc(e.ts), "level": e.level, "kind": e.kind, "market": e.market, "symbol": e.symbol,
             "message": e.message}
            for e in services.store.latest_events(EVENTS_SHOWN)
        ],
    })


def _app_state(services: Services, now: datetime) -> dict:
    """Whether each key is set, never the key (§19)."""
    config = services.config
    schedule = config.schedule
    times = {"US": schedule.us_update, "TW": schedule.tw_update}
    return {
        "server_time": _utc(now),
        "mode": config.mode,
        "keys": {"tiingo": bool(config.tiingo.api_key), "finmind": bool(config.finmind.token), "fred": bool(config.fred.api_key)},
        "services": {"spylead": _spylead(services, len)},
        "limits": None if config.mode == "test" or config.limits is None else asdict(config.limits),
        "schedule": {"enabled": schedule.enabled,
                     "next": {m: _utc(next_time(m, at, now)) for m, at in times.items()} if schedule.enabled else None},
    }


def _spylead(services: Services, series: Callable[[tuple[ExternalSeries, ...]], Any]) -> dict:
    """Whether spylead is configured, when the stored snapshot was taken, and its series as series() describes them
    (§5.6)."""
    snapshot = services.store.external_snapshot()
    return {"configured": bool(services.config.spylead.url),
            "snapshot_at": None if snapshot is None else _utc(snapshot.taken_at),
            "series": series(() if snapshot is None else snapshot.series)}


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


async def get_spylead(request: web.Request) -> web.Response:
    """The stored snapshot (§5.6): when it was taken, and per series its in-sample mark and the first and last day
    with a value."""
    return _json(_spylead(_services(request), lambda found: [_external(series) for series in found]))


def _external(found: ExternalSeries) -> dict:
    known = np.flatnonzero(np.isfinite(found.values))
    first, last = (found.first_day + int(known[0]), found.first_day + int(known[-1])) if len(known) else (None, None)
    return {"name": found.name, "in_sample": found.in_sample, "first_day": _iso(first), "last_day": _iso(last)}


async def snapshot(request: web.Request) -> web.Response:
    """202 with the job's lane and kind; 409 SPYLEAD_NOT_CONFIGURED without spylead's url, or JOB_RUNNING while the
    DATA lane is busy."""
    await read_json(request)
    services = _services(request)
    if not services.config.spylead.url:
        raise ApiError(409, "SPYLEAD_NOT_CONFIGURED", "spylead's url is empty in config.toml")
    job = services.jobs.snapshot()
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


def _defaults(market: str) -> dict:
    """A market's defaults for the run form (§4.1), as the configuration's validation fills them."""
    spec = MARKETS[market]
    fee = {"borrow_fee_pct": spec.borrow_fee_pct} if market == "US" else {"short_fee_pct": spec.short_fee_pct}
    return {"initial_capital": spec.initial_capital, "benchmark": spec.benchmark, "categories": list(spec.categories),
            "costs": {"commission": asdict(spec.commission), "slippage_bps": spec.slippage_bps,
                      "dividend_withholding_pct": spec.dividend_withholding_pct, **fee}}


def _strategies(folder: PluginFolder) -> dict:
    """Built-ins with their schema per market, plugins with theirs, the plugins that failed to load (§9.5), and each
    market's defaults for the run form."""
    return {
        "defaults": {market: _defaults(market) for market in MARKETS},
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
    """The registry of §8.6, with the stored external series; in_sample marks F and COMPOSITE (§5.6)."""
    external = stored_series(_services(request).store)
    return _json([
        {"name": o.name, "kind": o.kind, "outputs": list(o.outputs), "source": o.source, "timeframes": list(o.timeframes),
         "args": {name: {"type": a.type, "default": a.default, "min": a.min, "max": a.max} for name, a in o.args.items()},
         **({"in_sample": external[o.name].in_sample} if o.kind == "series" else {})}
        for o in operands(external).values()
    ])


def _valid_rule(rule: Any, store: Store) -> dict:
    issues = validate_rule(rule, stored_series(store))
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
    name, rule = _text(body, "name"), _valid_rule(body.get("rule"), services.store)
    try:
        rule_id = services.store.create_rule(name, rule, _now(services))
    except StoreError as error:
        raise _conflict(error, "a rule already has that name") from None
    return _json({"id": rule_id}, 201)


async def update_rule(request: web.Request) -> web.Response:
    services = _services(request)
    body = _object(await read_json(request), ("name", "rule"))
    name, rule = _text(body, "name"), _valid_rule(body.get("rule"), services.store)
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
    rule = _object(await read_json(request), ("rule",)).get("rule")
    issues = validate_rule(rule, stored_series(_services(request).store))
    return _json({"ok": not issues, "errors": _issues(issues)})


# ----- data payloads (§19) -----


def _round(number: float) -> float | None:
    """6 significant digits; NaN and infinities become null (§19)."""
    return float(f"{number:.6g}") if math.isfinite(number) else None


def _rounded(value: Any, keep: tuple[str, ...] = ()) -> Any:
    """Every float of a data payload rounded; the entries named in keep (parameter values) stay as stored."""
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, dict):
        return {key: item if key in keep else _rounded(item, keep) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(item, keep) for item in value]
    return value


def _column(values: np.ndarray) -> list[float | None]:
    return [_round(value) for value in np.asarray(values, dtype=np.float64).tolist()]


def _time_unit(timeframe: str) -> str:
    return "day" if timeframe == "1d" else "second"


def _query_integer(request: web.Request, key: str, default: int | None, low: int = 0, high: int | None = None) -> int | None:
    text = request.query.get(key)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ApiError(400, "INVALID_FIELD", "must be a whole number", key) from None
    if value < low or (high is not None and value > high):
        raise ApiError(400, "INVALID_FIELD", f"must be from {low}" + (f" to {high}" if high is not None else " up"), key)
    return value


def _invalid(error: RunConfigError, prefix: str = "") -> ApiError:
    """INVALID_FIELD at the first error's path, with every error listed."""
    errors = [{"path": prefix + path if path else prefix.rstrip("."), "message": message} for path, message in error.errors]
    return ApiError(400, "INVALID_FIELD", errors[0]["message"], errors[0]["path"], errors=errors)


def _job(job: Any) -> dict:
    return {"job": {"lane": job.lane, "kind": job.kind}}


# ----- backtests (§10.11, §12, §20.5) -----


def _backtest(store: Store, backtest_id: int) -> BacktestRow:
    found = store.backtest(backtest_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", "no such backtest")
    return found


async def start_backtest(request: web.Request) -> web.Response:
    """202 with the job; 400 INVALID_FIELD at the configuration's path; 409 JOB_RUNNING."""
    config = _object(await read_json(request), ("config",)).get("config")
    try:
        job = _services(request).jobs.backtest(config)
    except RunConfigError as error:
        raise _invalid(error, "config.") from None
    return _json(_job(job), 202)


async def list_backtests(request: web.Request) -> web.Response:
    """Newest first: what the results list shows of each."""
    market = request.query.get("market")
    if market is not None:
        _market(market, "market")
    limit = _query_integer(request, "limit", LIST_LIMIT, 1, MAX_LIST_LIMIT)
    return _json([
        {"id": row.id, "name": row.name, "created_at": _utc(row.created_at), "market": row.market,
         "timeframe": row.timeframe,
         "strategy": {"source": row.config["strategy"]["source"], "id": row.config["strategy"].get("id")},
         "period": {"start": row.config["start"], "end": row.config["end"]},
         "metrics": _rounded({key: row.metrics["strategy"].get(key) for key in KEY_METRICS}),
         "flags": list(row.flags), "keep_offline": row.keep_offline}
        for row in _services(request).store.backtests(market, limit)
    ])


async def get_backtest(request: web.Request) -> web.Response:
    """The detail; serving it sets opened_at, which chooses the results kept offline (§20.5). The app's offline sync
    (X-Btlab-Sync: 1) caches a result without opening it."""
    services = _services(request)
    if request.headers.get(SYNC_HEADER) == "1":
        row = services.store.backtest(_id(request))
    else:
        row = services.store.open_backtest(_id(request), _now(services))
    if row is None:
        raise ApiError(404, "NOT_FOUND", "no such backtest")
    return _json({
        "id": row.id, "name": row.name, "created_at": _utc(row.created_at), "market": row.market,
        "timeframe": row.timeframe, "time_unit": _time_unit(row.timeframe), "config": row.config,
        "data_stamp": row.data_stamp, "metrics": _rounded(row.metrics), "tables": _rounded(row.tables),
        "flags": row.flags, "keep_offline": row.keep_offline, "opened_at": _utc(row.opened_at),
    })


async def patch_backtest(request: web.Request) -> web.Response:
    body = _object(await read_json(request), ("name", "keep_offline"))
    name = _text(body, "name") if "name" in body else None
    keep = body.get("keep_offline")
    if "keep_offline" in body and not isinstance(keep, bool):
        raise ApiError(400, "INVALID_FIELD", "must be true or false", "keep_offline")
    if not _services(request).store.update_backtest(_id(request), name, keep):
        raise ApiError(404, "NOT_FOUND", "no such backtest")
    return web.Response(status=204)


async def delete_backtest(request: web.Request) -> web.Response:
    """409 IN_USE while an active live portfolio follows the result."""
    store = _services(request).store
    row = _backtest(store, _id(request))
    if store.backtest_in_use(row.id):
        raise ApiError(409, "IN_USE", "a live portfolio follows this backtest")
    store.delete_backtest(row.id)
    return web.Response(status=204)


async def get_series(request: web.Request) -> web.Response:
    """The series payload: the stored series of §18, and the drawdown of the equity."""
    store = _services(request).store
    row = _backtest(store, _id(request))
    series = store.backtest_series(row.id)
    shown = {kind: _column(series[kind]) for kind in SERIES_SHOWN if kind in series}
    shown["DRAWDOWN"] = _column(drawdown(series["EQUITY"]))
    return _json({"time_unit": _time_unit(row.timeframe), "t": series["TIME"].astype(np.int64).tolist(), "series": shown})


async def get_trades(request: web.Request) -> web.Response:
    store = _services(request).store
    row = _backtest(store, _id(request))
    offset = _query_integer(request, "offset", 0)
    limit = _query_integer(request, "limit", TRADES_LIMIT, 1, MAX_TRADES_LIMIT)
    return _json({"time_unit": _time_unit(row.timeframe), "total": store.backtest_trade_count(row.id), "offset": offset,
                  "trades": [_rounded(asdict(trade)) for trade in store.backtest_trades(row.id, offset, limit)]})


async def get_backtest_candles(request: web.Request) -> web.Response:
    """The run's symbol on the run's timeframe, on the prices its fills used (raw in REALISTIC mode), with the
    trades' entries and exits as markers."""
    services = _services(request)
    row = _backtest(services.store, _id(request))
    symbol = request.match_info["symbol"]
    if symbol not in row.config["symbols"]:
        raise ApiError(404, "NOT_FOUND", "the backtest has no such symbol")
    adjusted_prices = row.config["price_mode"] == "ADJUSTED"
    found = _candles(services, row.market, symbol, row.timeframe, adjusted_prices, *_window(request, row.timeframe, services))
    trades = [trade for trade in services.store.backtest_trades(row.id) if trade.symbol == symbol]
    splits = [] if adjusted_prices else [a for a in services.store.actions(row.market, symbol) if a.kind == "SHARE_RATIO"]
    return _json(found | {"markers": _markers(trades, found["t"], splits, _day_of(row.market, row.timeframe))})


def _markers(trades: list[Trade], times: list[int], splits: list, day_of: Callable[[int], int]) -> list[dict]:
    """Entries, and exits other than OPEN, inside the candles' window. A share event during a trade rescaled its
    average entry price and quantity (§10.7); on raw candles the entry shows as it was filled."""
    if not times:
        return []
    found = []
    for trade in trades:
        opening, closing = ("BUY", "SELL") if trade.side == "LONG" else ("SELL", "BUY")
        entered, ended = day_of(trade.entry_time), day_of(trade.exit_time if trade.exit_time is not None else trade.entry_time)
        factor = math.prod(split.ratio for split in splits if entered < split.day <= ended)
        moments = [(trade.entry_time, opening, trade.entry_price * factor, trade.quantity / factor, "ENTRY")]
        if trade.exit_reason != "OPEN":
            moments.append((trade.exit_time, closing, trade.exit_price, trade.quantity, trade.exit_reason))
        found += [{"t": moment, "side": side, "price": _round(price), "quantity": _round(quantity), "reason": reason}
                  for moment, side, price, quantity, reason in moments if times[0] <= moment <= times[-1]]
    return sorted(found, key=lambda marker: marker["t"])


def _day_of(market: str, timeframe: str) -> Callable[[int], int]:
    """A bar time's market day: itself on the daily timeline, the local date of an epoch second on minute ones."""
    if timeframe == "1d":
        return int
    zone = ZoneInfo(MARKETS[market].time_zone)
    return lambda moment: to_day(datetime.fromtimestamp(int(moment), zone).date())


async def export_trades(request: web.Request) -> web.Response:
    store = _services(request).store
    row = _backtest(store, _id(request))
    body = trades_csv(tuple(store.backtest_trades(row.id)), row.market, _time_unit(row.timeframe))
    return _csv(body, f"backtest-{row.id}-trades.csv")


async def export_equity(request: web.Request) -> web.Response:
    store = _services(request).store
    row = _backtest(store, _id(request))
    return _csv(equity_csv(store.backtest_series(row.id), row.market, _time_unit(row.timeframe)), f"backtest-{row.id}-equity.csv")


def _csv(body: bytes, name: str) -> web.Response:
    return web.Response(body=body, content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def get_offline(request: web.Request) -> web.Response:
    """The results the offline cache holds: the 20 most recently opened plus every keep-offline one (§20.5)."""
    return _json({"ids": _services(request).store.offline_backtest_ids(OFFLINE_RECENT)})


async def get_compare(request: web.Request) -> web.Response:
    """2 to 4 results: each curve divided by its first value, aligned on the union of their days (a minute run's
    session ends fall on their days), null where a run has no point; and their metrics side by side."""
    store = _services(request).store
    text = request.query.get("ids", "")
    try:
        ids = list(dict.fromkeys(int(part) for part in text.split(",")))
    except ValueError:
        raise ApiError(400, "INVALID_FIELD", "must be ids separated by commas", "ids") from None
    if not COMPARED[0] <= len(ids) <= COMPARED[1]:
        raise ApiError(400, "INVALID_FIELD", f"must name {COMPARED[0]} to {COMPARED[1]} backtests", "ids")
    rows = [_backtest(store, backtest_id) for backtest_id in ids]
    curves = [_curve_by_day(row, store.backtest_series(row.id)) for row in rows]
    days = np.unique(np.concatenate([found for found, _ in curves]))
    series = {}
    for row, (found, values) in zip(rows, curves):
        aligned = np.full(len(days), np.nan)
        aligned[np.searchsorted(days, found)] = values
        series[str(row.id)] = _column(aligned)
    return _json({
        "time_unit": "day", "t": days.tolist(), "series": series,
        "runs": [{"id": row.id, "name": row.name, "market": row.market, "timeframe": row.timeframe,
                  "metrics": _rounded(row.metrics["strategy"])} for row in rows],
    })


def _curve_by_day(row: BacktestRow, series: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    equity = series["EQUITY"]
    day_of = _day_of(row.market, row.timeframe)
    times = np.array([day_of(moment) for moment in series["TIME"].astype(np.int64).tolist()], dtype=np.int64)
    first = equity[np.isfinite(equity)][0]
    return times, equity / first


# ----- candles (§19) -----


def _window(request: web.Request, timeframe: str, services: Services) -> tuple[int, int]:
    """from and to in the payload's time unit; a minute window without from starts 40 days before to (now by
    default)."""
    last = _query_integer(request, "to", None)
    first = _query_integer(request, "from", None)
    if timeframe == "1d":
        return (0 if first is None else first), (np.iinfo(np.int32).max if last is None else last)
    last = int(services.clock()) if last is None else last
    return (last - MINUTE_WINDOW_S if first is None else first), last


async def get_candles(request: web.Request) -> web.Response:
    """A stored symbol's candles: daily, or minute bars resampled to the timeframe; adjusted unless told otherwise."""
    services = _services(request)
    market, symbol = _known_symbol(request)
    timeframe = request.query.get("tf", "1d")
    if timeframe not in TIMEFRAMES:
        raise ApiError(400, "INVALID_FIELD", "must be one of " + ", ".join(TIMEFRAMES), "tf")
    choice = request.query.get("adjusted", "true")
    if choice not in ("true", "false"):
        raise ApiError(400, "INVALID_FIELD", "must be true or false", "adjusted")
    found = _candles(services, market, symbol, timeframe, choice == "true", *_window(request, timeframe, services))
    return _json(found | {"markers": []})


def _candles(services: Services, market: str, symbol: str, timeframe: str, adjust: bool, first: int, last: int) -> dict:
    """The candle payload without markers: at most 5,000 bars, the last ones of [first, last]."""
    store = services.store
    daily = store.daily_bars(market, symbol)
    actions = store.actions(market, symbol)
    if timeframe == "1d":
        bars = adjusted(daily, day_events(market, daily, actions)[0]) if adjust else daily
        t, columns = bars.day, [bars.open, bars.high, bars.low, bars.close, bars.volume]
    else:
        t, columns = _minute_candles(services.minute_store, market, symbol, timeframe, daily, actions, adjust, first, last)
    inside = np.flatnonzero((t >= first) & (t <= last))[-MAX_CANDLES:]
    names = ("open", "high", "low", "close", "volume")
    return {"time_unit": _time_unit(timeframe), "t": t[inside].astype(np.int64).tolist(),
            **{name: _column(values[inside]) for name, values in zip(names, columns)}}


def _minute_candles(minute_store: MinuteStore, market: str, symbol: str, timeframe: str, daily: Any, actions: list,
                    adjust: bool, first: int, last: int) -> tuple[np.ndarray, list[np.ndarray]]:
    """1-minute bars resampled per session (§11.1); adjusted by their day's scale, as on minute timelines (§11.2)."""
    bars = minute_store.minute_bars(market, symbol, first, last)
    day_of = _day_of(market, timeframe)
    days = np.array([day_of(moment) for moment in bars.t.tolist()], dtype=np.int64)
    opens = {day: session(market, day)[0] for day in np.unique(days).tolist()}
    bars = resample(bars, np.array([opens[day] for day in days.tolist()], dtype=np.int64), TIMEFRAME_MINUTES[timeframe])
    prices = [bars.open, bars.high, bars.low, bars.close]
    if not adjust or not len(bars.t):
        return bars.t, [*prices, bars.volume]
    aligned, _ = align(market, daily.day, {symbol: (daily, actions)})
    row = np.searchsorted(aligned.days, [day_of(moment) for moment in bars.t.tolist()])
    with np.errstate(divide="ignore", invalid="ignore"):
        volume_scale = aligned.adj_volume[:, 0] / aligned.volume[:, 0]
    volume_scale = np.where(np.isfinite(volume_scale) & (volume_scale > 0), volume_scale, 1.0)
    scale = aligned.multiplier[np.minimum(row, len(aligned.days) - 1), 0]
    return bars.t, [*(price * scale for price in prices), bars.volume * volume_scale[np.minimum(row, len(aligned.days) - 1)]]


# ----- optimizations (§13) -----


def _optimization(store: Store, optimization_id: int) -> OptimizationRow:
    found = store.optimization(optimization_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", "no such optimization")
    return found


def _optimization_summary(row: OptimizationRow) -> dict:
    return {"id": row.id, "created_at": _utc(row.created_at), "name": row.name, "market": row.market, "mode": row.mode,
            "status": row.status, "best": _rounded(row.best, ("values",)), "flags": row.flags}


async def start_optimization(request: web.Request) -> web.Response:
    """400 INVALID_FIELD or TOO_MANY_COMBOS before any job; once the job has its estimate, 409 NEEDS_CONFIRMATION
    when it exceeds 10 minutes without confirm_long (§13.6), else 202."""
    body = await read_json(request)
    try:
        job = _services(request).jobs.optimization(body)
    except RunConfigError as error:
        raise _invalid(error) from None
    except OptimizeError as error:
        raise ApiError(400, error.code, str(error)) from None
    ready = await job.ready
    if isinstance(ready, NeedsConfirmation):
        raise ApiError(409, "NEEDS_CONFIRMATION", "the optimization needs more than 10 minutes; confirm it",
                       estimate_seconds=_round(ready.estimate_seconds))
    return _json(_job(job), 202)


async def list_optimizations(request: web.Request) -> web.Response:
    return _json([_optimization_summary(row) for row in _services(request).store.optimizations()])


async def get_optimization(request: web.Request) -> web.Response:
    row = _optimization(_services(request).store, _id(request))
    return _json(_optimization_summary(row) | {"config": row.config, "results": _rounded(row.results, ("values", "paths"))})


async def get_heatmap(request: web.Request) -> web.Response:
    """§13.3; 400 INVALID_FIELD when x and y are not two of the optimization's paths."""
    row = _optimization(_services(request).store, _id(request))
    try:
        found = heatmap(row.config, row.results, request.query.get("x"), request.query.get("y"))
    except ValueError as error:
        raise ApiError(400, "INVALID_FIELD", str(error), "x") from None
    return _json(_rounded(found, ("values",)))


async def promote(request: web.Request) -> web.Response:
    """A combination, given as its values, runs as an ordinary backtest (§13.7)."""
    services = _services(request)
    row = _optimization(services.store, _id(request))
    paths = list(row.config["ranges"])
    combo = _object(await read_json(request), ("combo",)).get("combo")
    if not isinstance(combo, list) or len(combo) != len(paths):
        raise ApiError(400, "INVALID_FIELD", f"must list {len(paths)} values, one per range", "combo")
    config = combination_config(row.config["config"], paths, combo)
    config["name"] = f"{config['name']} ({', '.join(f'{path} {value}' for path, value in zip(paths, combo))})"
    try:
        job = services.jobs.backtest(config)
    except RunConfigError as error:
        path, message = error.errors[0]
        raise ApiError(400, "INVALID_FIELD", f"{path}: {message}", "combo") from None
    return _json(_job(job), 202)


async def delete_optimization(request: web.Request) -> web.Response:
    if not _services(request).store.delete_optimization(_id(request)):
        raise ApiError(404, "NOT_FOUND", "no such optimization")
    return web.Response(status=204)


# ----- static files (§20) -----


async def static(request: web.Request) -> web.FileResponse:
    """The UI's files, without a session; / is index.html, and nothing outside the folder is served."""
    path = (STATIC / (request.match_info["path"] or "index.html")).resolve()
    if not path.is_relative_to(STATIC) or not path.is_file():
        raise web.HTTPNotFound()
    content_type = CONTENT_TYPES.get(path.suffix)
    return web.FileResponse(path, headers={"Content-Type": content_type} if content_type else None)
