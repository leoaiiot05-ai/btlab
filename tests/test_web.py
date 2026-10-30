"""The JSON API core: sessions, bodies, errors, headers, routes and the state document (SPEC.md §19, §21, AC-19, AC-20)."""

import asyncio
import logging
from datetime import datetime, time, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from btlab.config import load_config
from btlab.jobs import JobRunning
from btlab.plugins import PluginFolder
from btlab.records import ExternalSeries, ExternalSnapshot, MinuteCoverage, to_day
from btlab.store import MinuteStore, Store
from btlab.web import COOKIE, Services, make_app, session_value, start_site
from builders import WEEKDAYS, put_bars
from test_config import SECRETS, VALID, edited, render
from test_plugins import VECTOR, write
from test_rules import base as valid_rule

TOKEN = VALID["web"]["token"]
NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
PROGRESS = {"kind": "UPDATE_US", "phase": "BARS", "done": 1, "total": 2, "item": "AAA",
            "started_at": "2026-10-28T01:00:00+00:00", "message": None}


class StubJobs:
    """Stands in for btlab.jobs.Jobs: records what the routes ask; busy lanes refuse a job."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.updates: list[str] = []
        self.snapshots = 0
        self.backfills: list[str | None] = []
        self.ib = None
        self.cancelled: list[str] = []
        self.failure: Exception | None = None

    def running(self) -> dict:
        return {lane: PROGRESS for lane in self.busy}

    def update(self, market: str) -> SimpleNamespace:
        if self.failure is not None:
            raise self.failure
        if "DATA" in self.busy:
            raise JobRunning("DATA")
        self.updates.append(market)
        return SimpleNamespace(lane="DATA", kind=f"UPDATE_{market}")

    def snapshot(self) -> SimpleNamespace:
        if "DATA" in self.busy:
            raise JobRunning("DATA")
        self.snapshots += 1
        return SimpleNamespace(lane="DATA", kind="SPYLEAD_SNAPSHOT")

    def backfill(self, market: str | None = None) -> SimpleNamespace:
        if "MINUTE" in self.busy:
            raise JobRunning("MINUTE")
        self.backfills.append(market)
        return SimpleNamespace(lane="MINUTE", kind="MINUTE_BACKFILL")

    def cancel(self, lane: str) -> bool:
        self.cancelled.append(lane)
        return lane in self.busy


@pytest.fixture
def world(tmp_path):
    found = make_world(tmp_path, VALID)
    yield found
    found.store.close()
    found.minute.close()


def make_world(tmp_path, document) -> SimpleNamespace:
    """The services of a web app over temporary stores, with a stub jobs object and a recorded sleep."""
    (tmp_path / "config.toml").write_text(render(document), encoding="utf-8")
    folder = tmp_path / "strategies"
    folder.mkdir(exist_ok=True)
    write(folder, "vector", VECTOR)
    (folder / "broken.py").write_text("def signals(:\n", encoding="utf-8")
    store, minute = Store(tmp_path / "data"), MinuteStore(tmp_path / "data")
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    jobs = StubJobs()
    services = Services(load_config(tmp_path / "config.toml"), store, minute, jobs, PluginFolder(folder),
                        lambda: NOW.timestamp(), sleep)
    return SimpleNamespace(services=services, store=store, minute=minute, jobs=jobs, sleeps=sleeps, folder=folder)


def serve(world, scenario, login: bool = True):
    """Runs scenario(client) against a test server of the app, logged in unless told otherwise."""
    async def go():
        async with TestClient(TestServer(make_app(world.services))) as client:
            if login:
                assert (await client.post("/api/login", json={"token": TOKEN})).status == 204
            return await scenario(client)

    return asyncio.run(go())


async def answer(response) -> tuple[int, object]:
    if response.content_type == "application/json":
        return response.status, await response.json()
    return response.status, await response.text()


# ----- AC-19: the session, bodies, headers, listening and secrets -----


def api_routes(world) -> list[tuple[str, str]]:
    fill = {"{id}": "1", "{market}": "US", "{symbol}": "AAA", "{lane}": "DATA"}
    found = []
    for route in make_app(world.services).router.routes():
        path = route.resource.canonical
        # aiohttp adds a HEAD route for each GET one; the GET stands for both.
        if path.startswith("/api/") and path != "/api/login" and route.method != "HEAD":
            for placeholder, value in fill.items():
                path = path.replace(placeholder, value)
            found.append((route.method, path))
    return found


def test_every_api_route_but_login_needs_the_session(world):
    routes = api_routes(world)
    assert len(routes) == 41

    async def scenario(client):
        return [await answer(await client.request(method, path, json={})) for method, path in routes]

    for status, body in serve(world, scenario, login=False):
        assert (status, body["error"]) == (401, "UNAUTHORIZED")


@pytest.mark.parametrize(
    "method, path, data, content_type, status, error, path_in_error",
    [
        ("POST", "/api/lists", '{"name": "x", "market": "US"}', "text/plain", 415, "UNSUPPORTED_MEDIA_TYPE", None),
        ("POST", "/api/lists", '{"name": ', "application/json", 400, "INVALID_JSON", None),
        ("POST", "/api/rules/validate", '{"rule": {"entry": {"value": NaN}}}', "application/json", 400, "INVALID_FIELD",
         "rule.entry.value"),
        ("POST", "/api/lists", '{"name": "x", "market": "US", "n": [1, Infinity]}', "application/json", 400,
         "INVALID_FIELD", "n[1]"),
        ("POST", "/api/lists", '{"name": "x", "market": "US", "n": 1e999}', "application/json", 400, "INVALID_FIELD", "n"),
        ("PATCH", "/api/symbols/US/SPY", '{"borrow_fee_pct": true}', "application/json", 400, "INVALID_FIELD",
         "borrow_fee_pct"),
    ],
)
def test_bodies_are_json_with_finite_numbers(world, method, path, data, content_type, status, error, path_in_error):
    world.store.add_symbol("US", "SPY", "ETF", NOW)

    async def scenario(client):
        return await answer(await client.request(method, path, data=data, headers={"Content-Type": content_type}))

    found_status, body = serve(world, scenario)
    assert (found_status, body["error"], body.get("path")) == (status, error, path_in_error)
    assert isinstance(body["message"], str)


def test_section_21_headers_on_api_answers_errors_and_static_files(world):
    async def scenario(client):
        found = {}
        for name, path in (("api", "/api/state"), ("error", "/api/nothing"), ("static", "/"), ("worker", "/sw.js"),
                           ("missing", "/nothing.js")):
            response = await client.get(path)
            found[name] = (response.status, response.headers)
        response = await client.post("/api/logout", json={})
        found["logout"] = (response.status, response.headers)
        found["unauthorized"] = ((r := await client.get("/api/state")).status, r.headers)
        return found

    found = serve(world, scenario)
    assert {name: status for name, (status, _) in found.items()} == {
        "api": 200, "error": 404, "static": 200, "worker": 200, "missing": 404, "logout": 204, "unauthorized": 401}
    for name, (_, headers) in found.items():
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"
        assert headers["Cache-Control"] == ("no-cache" if name in ("static", "worker", "missing") else "no-store"), name
    # A service worker served as anything but JavaScript is refused; no-cache finds a new one at once.
    assert found["worker"][1]["Content-Type"].startswith("text/javascript")


def test_the_server_listens_on_127_0_0_1_only(world):
    async def go():
        runner = await start_site(make_app(world.services), 0)
        try:
            return runner.addresses
        finally:
            await runner.cleanup()

    (address,) = asyncio.run(go())
    assert address[0] == "127.0.0.1"


@pytest.mark.parametrize("forwarded, secure", [(None, False), ("https", True)])
def test_the_session_cookie_flags(world, forwarded, secure):
    async def scenario(client):
        headers = {"X-Forwarded-Proto": forwarded} if forwarded else {}
        response = await client.post("/api/login", json={"token": TOKEN}, headers=headers)
        return response.status, response.headers["Set-Cookie"]

    status, cookie = serve(world, scenario, login=False)
    parts = [part.strip() for part in cookie.split(";")]
    assert status == 204 and parts[0] == f"{COOKIE}={session_value(TOKEN)}"
    assert {"HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=Strict"} <= set(parts)
    assert ("Secure" in parts) == secure


def test_no_secret_reaches_logs_events_answers_or_errors(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.store.add_symbol("US", "SPY", "ETF", NOW)
    world.jobs.failure = RuntimeError("an unexpected failure")

    async def scenario(client):
        texts = []
        for method, path, body in [
            ("POST", "/api/login", {"token": "wrong"}), ("GET", "/api/state", None), ("GET", "/api/symbols?market=US", None),
            ("GET", "/api/strategies", None), ("POST", "/api/update", {"market": "US"}),
            ("PATCH", "/api/symbols/US/SPY", {"category": "ETF"}), ("GET", "/api/nothing", None),
        ]:
            response = await client.request(method, path, json=body)
            texts.append(await response.text())
            texts.append(str({k: v for k, v in response.headers.items() if k != "Set-Cookie"}))
        return texts

    texts = serve(world, scenario)
    events = [event.message for event in world.store.latest_events(50)]
    assert "INTERNAL_ERROR" in [event.kind for event in world.store.latest_events(50)]
    for secret in [*SECRETS, session_value(TOKEN)]:
        assert all(secret not in text for text in texts) and secret not in caplog.text
        assert all(secret not in message for message in events)


def test_login_waits_a_second_on_a_wrong_token_and_logout_clears_the_session(world):
    async def scenario(client):
        wrong = await answer(await client.post("/api/login", json={"token": "Sentinel-Not-The-Token"}))
        missing = await answer(await client.post("/api/login", json={}))
        sleeps = list(world.sleeps)
        right = (await client.post("/api/login", json={"token": TOKEN})).status
        inside = (await client.get("/api/state")).status
        logout = await client.post("/api/logout", json={})
        after = (await client.get("/api/state")).status
        return wrong, missing, sleeps, right, inside, logout.status, logout.headers["Set-Cookie"], after

    wrong, missing, sleeps, right, inside, out, cleared, after = serve(world, scenario, login=False)
    assert wrong[0] == missing[0] == 401 and wrong[1]["error"] == missing[1]["error"] == "LOGIN_FAILED"
    assert sleeps == [1.0, 1.0] and world.sleeps == [1.0, 1.0]  # none for the right token
    assert (right, inside, out, after) == (204, 200, 204, 401)
    assert "Max-Age=0" in cleared


def test_unknown_paths_and_methods(world):
    async def scenario(client):
        return [await answer(await client.request(method, path, json={})) for method, path in
                (("GET", "/api/nothing"), ("DELETE", "/api/lists"), ("PUT", "/api/lists/9"), ("DELETE", "/api/rules/9"))]

    found = serve(world, scenario)
    assert [(status, body["error"]) for status, body in found] == [
        (404, "NOT_FOUND"), (405, "METHOD_NOT_ALLOWED"), (404, "NOT_FOUND"), (404, "NOT_FOUND")]


def test_an_unexpected_error_is_answered_500_and_recorded(world):
    world.jobs.failure = RuntimeError("an unexpected failure")

    async def scenario(client):
        return await answer(await client.post("/api/update", json={"market": "US"}))

    status, body = serve(world, scenario)
    assert (status, body["error"]) == (500, "INTERNAL_ERROR")
    (event,) = world.store.latest_events(1)
    assert (event.kind, event.message) == ("INTERNAL_ERROR", "POST /api/update: RuntimeError")


def test_static_files_need_no_session_and_stay_in_their_folder(world):
    async def scenario(client):
        return [(await client.get(path)).status for path in ("/", "/index.html", "/vendor/echarts/LICENSE", "/vendor/../../web.py")]

    assert serve(world, scenario, login=False) == [200, 200, 200, 404]


# ----- the routes of this prompt (AC-20) -----


def test_lists_round_trip(world):
    async def scenario(client):
        steps = [
            await answer(await client.post("/api/lists", json={"name": "tech", "market": "US"})),
            await answer(await client.post("/api/lists", json={"name": "tech", "market": "US"})),
            await answer(await client.post("/api/lists", json={"name": "jp", "market": "JP"})),
            await answer(await client.post("/api/lists", json={"name": "台灣", "market": "TW"})),
            await answer(await client.put("/api/lists/1", json={"symbols": ["aapl", "brk.b"]})),
            await answer(await client.put("/api/lists/1", json={"symbols": ["AAPL", "not a symbol"]})),
            await answer(await client.put("/api/lists/1", json={"symbols": ["AAPL", "aapl"]})),
            await answer(await client.put("/api/lists/1", json={"name": "台灣"})),
            await answer(await client.put("/api/lists/9", json={"name": "x"})),
            await answer(await client.get("/api/lists")),
            await answer(await client.get("/api/symbols?market=US")),
            await answer(await client.delete("/api/lists/2")),
            await answer(await client.delete("/api/lists/2")),
        ]
        return steps

    steps = serve(world, scenario)
    assert steps[0] == (201, {"id": 1})
    assert [(status, body["error"], body.get("path")) for status, body in steps[1:3]] == [
        (409, "ALREADY_EXISTS", None), (400, "INVALID_FIELD", "market")]
    assert steps[3][0] == 201 and steps[4] == (204, "")
    assert [(status, body["error"], body.get("path")) for status, body in steps[5:9]] == [
        (400, "INVALID_FIELD", "symbols[1]"), (400, "INVALID_FIELD", "symbols[1]"), (409, "ALREADY_EXISTS", None),
        (404, "NOT_FOUND", None)]
    lists = steps[9][1]
    assert [(found["id"], found["name"], found["market"], found["symbols"]) for found in lists] == [
        (1, "tech", "US", ["AAPL", "BRK.B"]), (2, "台灣", "TW", [])]
    assert [(row["symbol"], row["category"], row["last_day"]) for row in steps[10][1]] == [
        ("AAPL", "STOCK", None), ("BRK.B", "STOCK", None)]
    assert [status for status, _ in steps[11:]] == [204, 404]


def test_symbols_show_their_data_take_settings_and_purge_when_unlisted(world):
    store, minute = world.store, world.minute
    put_bars(store, "US", "AAA", WEEKDAYS[:3], np.array([10.0, 11.0, 12.0]))
    store.record_fetch("US", "AAA", NOW, "HTTP_500: Tiingo answered 500: prices of AAA")
    minute.save_minute_day("US", "AAA", MinuteCoverage(int(WEEKDAYS[0]), "FAILED", None, NOW))
    minute.save_minute_day("US", "AAA", MinuteCoverage(int(WEEKDAYS[1]), "EMPTY", None, NOW))
    put_bars(store, "TW", "2330", WEEKDAYS[:2], np.array([600.0, 601.0]))
    listed = store.create_list("mine", "US", NOW)
    store.set_list_symbols(listed, ["AAA"])

    async def scenario(client):
        return [
            await answer(await client.get("/api/symbols?market=US")),
            await answer(await client.get("/api/symbols")),
            await answer(await client.patch("/api/symbols/US/AAA", json={"category": "ETF", "minute_enabled": True,
                                                                          "borrow_fee_pct": 1.5})),
            await answer(await client.patch("/api/symbols/US/AAA", json={"category": "TDR"})),
            await answer(await client.patch("/api/symbols/TW/2330", json={"borrow_fee_pct": 1.0})),
            await answer(await client.patch("/api/symbols/US/ZZZ", json={"category": "ETF"})),
            await answer(await client.delete("/api/symbols/US/AAA")),
            await answer(await client.put(f"/api/lists/{listed}", json={"symbols": []})),
            await answer(await client.delete("/api/symbols/US/AAA")),
            await answer(await client.get("/api/symbols?market=US")),
        ]

    steps = serve(world, scenario)
    (row,) = steps[0][1]
    assert row == {
        "symbol": "AAA", "name": None, "category": "STOCK", "category_source": "RULE", "exchange": None,
        "first_day": "2019-01-01", "last_day": "2019-01-03", "last_fetch_at": "2026-10-28T01:00:00Z",
        "last_error": "HTTP_500: Tiingo answered 500: prices of AAA", "minute_enabled": False, "borrow_fee_pct": None,
        "coverage": {"FAILED": 1, "EMPTY": 1},
    }
    assert (steps[1][0], steps[1][1]["path"]) == (400, "market")
    assert steps[2] == (204, "")
    assert [(status, body["error"], body.get("path")) for status, body in steps[3:7]] == [
        (400, "INVALID_FIELD", "category"), (400, "INVALID_FIELD", "borrow_fee_pct"), (404, "NOT_FOUND", None),
        (409, "IN_USE", None)]
    assert [status for status, _ in steps[7:9]] == [204, 204] and steps[9] == (200, [])
    assert len(store.daily_bars("US", "AAA").day) == 0 and minute.minute_coverage("US", "AAA") == []
    changes = [e.message for e in store.latest_events(10) if e.kind == "SETTING_CHANGED"]
    assert changes == ["AAA: borrow fee 1.5%", "AAA: minute bars on", "AAA: category ETF"]


def test_update_and_cancel(world):
    async def scenario(client):
        found = [await answer(await client.post("/api/update", json={"market": "US"})),
                 await answer(await client.post("/api/update", json={"market": "JP"}))]
        world.jobs.busy.add("DATA")
        found += [await answer(await client.post("/api/update", json={"market": "TW"}))]
        for lane in ("DATA", "LIVE", "NOPE"):
            found.append(await answer(await client.post(f"/api/jobs/{lane}/cancel", json={})))
        return found

    steps = serve(world, scenario)
    assert steps[0] == (202, {"lane": "DATA", "kind": "UPDATE_US"}) and world.jobs.updates == ["US"]
    assert [(status, body["error"]) for status, body in steps[1:3]] == [(400, "INVALID_FIELD"), (409, "JOB_RUNNING")]
    assert steps[3] == (204, "") and world.jobs.cancelled == ["DATA"]
    assert [(status, body["error"]) for status, body in steps[4:]] == [(409, "NOT_CANCELLABLE"), (404, "NOT_FOUND")]


def test_strategies_list_built_ins_plugins_and_load_errors_and_reload(world):
    async def scenario(client):
        first = await answer(await client.get("/api/strategies"))
        (world.folder / "broken.py").unlink()
        return first, await answer(await client.post("/api/strategies/reload", json={}))

    (status, found), (reloaded_status, reloaded) = serve(world, scenario)
    assert status == reloaded_status == 200
    assert [b["id"] for b in found["builtins"]] == ["MA_CROSS", "RSI", "MACD", "BOLLINGER", "BREAKOUT", "BUY_HOLD", "DCA",
                                                    "MOMENTUM_ROTATION", "DUAL_MOMENTUM", "ZSCORE_REVERSION"]
    dca = next(b for b in found["builtins"] if b["id"] == "DCA")
    assert (dca["params"]["US"]["amount"]["default"], dca["params"]["TW"]["amount"]["default"]) == (1000.0, 30000.0)
    (plugin,) = found["plugins"]
    assert (plugin["id"], plugin["timeframe"], plugin["style"], plugin["output"]) == ("vector", "1d", "vector", "WEIGHTS")
    assert plugin["params"]["n"] == {"type": "int", "default": 3, "min": 1, "max": 50, "step": None,
                                     "label_en": "", "label_zh": ""}
    assert [error["id"] for error in found["errors"]] == ["broken"] and reloaded["errors"] == []


def test_indicators_list_the_registry(world):
    async def scenario(client):
        return await answer(await client.get("/api/indicators"))

    status, found = serve(world, scenario)
    by_name = {item["name"]: item for item in found}
    assert status == 200 and len(found) == 26
    assert by_name["SMA"] == {"name": "SMA", "kind": "ind", "outputs": [], "source": "close",
                              "timeframes": ["1d", "1m", "5m", "15m", "30m", "60m"],
                              "args": {"n": {"type": "int", "default": 20, "min": 1, "max": 1000}}}
    assert by_name["CLOSE_VS_VWAP"]["timeframes"] == ["1d"] and by_name["TODAY_OPEN"]["kind"] == "today"


def test_rules_round_trip_with_validation(world):
    rule = valid_rule()
    broken = valid_rule(version=2)

    async def scenario(client):
        return [
            await answer(await client.post("/api/rules", json={"name": "trend", "rule": broken})),
            await answer(await client.post("/api/rules", json={"name": "trend", "rule": rule})),
            await answer(await client.post("/api/rules", json={"name": "trend", "rule": rule})),
            await answer(await client.post("/api/rules", json={"name": " ", "rule": rule})),
            await answer(await client.put("/api/rules/1", json={"name": "trend 2", "rule": {**rule, "side": "short"}})),
            await answer(await client.put("/api/rules/9", json={"name": "x", "rule": rule})),
            await answer(await client.get("/api/rules")),
            await answer(await client.post("/api/rules/validate", json={"rule": rule})),
            await answer(await client.post("/api/rules/validate", json={"rule": broken})),
            await answer(await client.delete("/api/rules/1")),
            await answer(await client.delete("/api/rules/1")),
        ]

    steps = serve(world, scenario)
    status, body = steps[0]
    assert (status, body["error"], body["errors"]) == (400, "RULE_INVALID",
                                                       [{"code": "INVALID_FIELD", "path": "version", "message": "must be 1"}])
    assert steps[1] == (201, {"id": 1})
    assert [(status, body["error"]) for status, body in steps[2:4]] == [(409, "ALREADY_EXISTS"), (400, "INVALID_FIELD")]
    assert steps[4] == (204, "") and (steps[5][0], steps[5][1]["error"]) == (404, "NOT_FOUND")
    (stored,) = steps[6][1]
    assert (stored["id"], stored["name"], stored["rule"]["side"], stored["updated_at"]) == (1, "trend 2", "short",
                                                                                           "2026-10-28T01:00:00Z")
    assert steps[7] == (200, {"ok": True, "errors": []}) and steps[8][1]["ok"] is False
    assert [status for status, _ in steps[9:]] == [204, 404]


def test_the_state_document(world):
    store = world.store
    store.set_data_start("US", to_day(datetime(1995, 10, 27).date()))
    put_bars(store, "US", "SPY", WEEKDAYS[-3:], np.array([1.0, 2.0, 3.0]))
    run = store.start_job_run("DATA", "UPDATE_US", NOW)
    store.finish_job_run(run, NOW, "UPDATED", "bars 3")
    for k in range(55):
        store.append_event(NOW, "INFO", "UP_TO_DATE", f"event {k}", "US")
    world.jobs.busy.add("DATA")

    async def scenario(client):
        return await answer(await client.get("/api/state"))

    status, found = serve(world, scenario)
    assert status == 200 and set(found) == {"app", "markets", "jobs", "events"}
    assert found["app"] == {"server_time": "2026-10-28T01:00:00Z", "mode": "test",
                            "keys": {"tiingo": True, "finmind": True, "fred": True},
                            "services": {
                                "ib": {"configured": True, "connected": False, "account_kind": None,
                                       "permissions": {"US": None, "TW": None}},
                                "spylead": {"configured": True, "snapshot_at": None, "series": 0}},
                            "limits": None, "schedule": {"enabled": False, "next": None}}
    assert found["markets"] == {
        "US": {"data_start": "1995-10-27", "last_day": "2020-12-31", "symbols": 1,
               "last_update": {"outcome": "UPDATED", "finished_at": "2026-10-28T01:00:00Z", "message": "bars 3"}},
        "TW": {"data_start": None, "last_day": None, "symbols": 0, "last_update": None},
    }
    assert found["jobs"] == {"DATA": PROGRESS, "MINUTE": None, "COMPUTE": None, "LIVE": None}
    events = found["events"]
    assert len(events) == 50 and events[0]["message"] == "event 54" and events[-1]["message"] == "event 5"
    assert set(events[0]) == {"id", "ts", "level", "kind", "market", "symbol", "message"}


def test_the_state_document_shows_the_next_updates_and_trade_limits(tmp_path):
    document = edited("schedule", "enabled", True)
    document["mode"] = "trade"
    document["limits"] = {"us_max_orders_per_day": 5, "tw_max_orders_per_day": 6, "max_order_pct_of_equity": 10.0,
                          "us_max_order_value": 1000.0, "tw_max_order_value": 30000.0}
    world = make_world(tmp_path, document)
    try:
        async def scenario(client):
            return await answer(await client.get("/api/state"))

        status, found = serve(world, scenario)
    finally:
        world.store.close()
        world.minute.close()
    # 09:00 on Wednesday in Taipei: the US update at 08:35 runs again on Thursday, the TW one at 18:05 today.
    assert found["app"]["schedule"] == {"enabled": True,
                                        "next": {"US": "2026-10-29T00:35:00Z", "TW": "2026-10-28T10:05:00Z"}}
    assert found["app"]["limits"]["us_max_orders_per_day"] == 5 and found["app"]["mode"] == "trade"


# ----- spylead (§5.6, AC-23) -----


def test_spylead_routes_the_state_and_the_registry_show_the_stored_series(world):
    first = to_day(datetime(2026, 1, 1).date())
    world.store.replace_external_series(ExternalSnapshot(NOW, (
        ExternalSeries("F", first, np.array([np.nan, 0.5, 0.25, np.nan]), True),
        ExternalSeries("SPYL1", first, np.array([np.nan, np.nan]), False))))
    reads_f = {"version": 1, "timeframe": "1d", "side": "long", "params": {},
               "entry": {"left": {"series": "F"}, "op": "gt", "right": {"value": 0}},
               "exit": {"left": {"series": "F"}, "op": "le", "right": {"value": 0}}}

    async def scenario(client):
        found = {
            "spylead": await answer(await client.get("/api/spylead")),
            "start": await answer(await client.post("/api/spylead/snapshot", json={})),
            "registry": await answer(await client.get("/api/indicators")),
            "valid": await answer(await client.post("/api/rules/validate", json={"rule": reads_f})),
            "state": await answer(await client.get("/api/state")),
        }
        world.jobs.busy.add("DATA")
        found["busy"] = await answer(await client.post("/api/spylead/snapshot", json={}))
        return found

    found = serve(world, scenario)
    assert found["spylead"] == (200, {"configured": True, "snapshot_at": "2026-10-28T01:00:00Z", "series": [
        {"name": "F", "in_sample": True, "first_day": "2026-01-02", "last_day": "2026-01-03"},
        {"name": "SPYL1", "in_sample": False, "first_day": None, "last_day": None}]})
    assert found["start"] == (202, {"lane": "DATA", "kind": "SPYLEAD_SNAPSHOT"}) and world.jobs.snapshots == 1
    series = [(o["name"], o["in_sample"], o["timeframes"]) for o in found["registry"][1] if o["kind"] == "series"]
    assert series == [("F", True, ["1d", "1m", "5m", "15m", "30m", "60m"]), ("SPYL1", False, ["1d", "1m", "5m", "15m", "30m", "60m"])]
    assert found["valid"] == (200, {"ok": True, "errors": []})
    assert found["state"][1]["app"]["services"]["spylead"] == {"configured": True, "snapshot_at": "2026-10-28T01:00:00Z",
                                                                "series": 2}
    assert found["busy"][0] == 409 and found["busy"][1]["error"] == "JOB_RUNNING"


def test_a_snapshot_needs_spylead_configured(tmp_path):
    world = make_world(tmp_path, edited("spylead", "url", ""))
    try:
        async def scenario(client):
            return [await answer(await client.post("/api/spylead/snapshot", json={})),
                    await answer(await client.get("/api/spylead"))]

        refused, shown = serve(world, scenario)
    finally:
        world.store.close()
        world.minute.close()
    assert refused[0] == 409 and refused[1]["error"] == "SPYLEAD_NOT_CONFIGURED" and world.jobs.snapshots == 0
    assert shown == (200, {"configured": False, "snapshot_at": None, "series": []})
