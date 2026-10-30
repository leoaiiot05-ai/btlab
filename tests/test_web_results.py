"""The API for results, candles, comparisons, exports and optimizations (SPEC.md §19, §20.5, AC-20, AC-22, AC-30).

The jobs are real, over the in-process pool; the stores are temporary.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from btlab.backtest import load_run, run_backtest, save_result, strategy_factory, validate_config
from btlab.config import load_config
from btlab.jobs import JobRunning, Jobs, Keys
from btlab.plugins import PluginFolder
from btlab.prices import adjusted, day_events
from btlab.records import DailyBars, MinuteCoverage, to_date, to_day
from btlab.store import MinuteStore
from btlab.web import Services, make_app
from builders import TAXES, WEEKDAYS, put_bars, synthetic_store
from fakes import InlinePool
from test_config import VALID, render
from test_intraday import minute_config, minutes_of
from test_plugins import PEEKING, write

TOKEN = VALID["web"]["token"]
NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)
CONFIG = {"name": "trend", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-06-03",
          "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 10, "slow": 30}}}


class Clock:
    def __init__(self, step: float = 0.0) -> None:
        self.now, self.step = NOW.timestamp(), step

    def clock(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def world(tmp_path):
    found = make_world(tmp_path)
    yield found
    found.store.close()
    found.minute.close()


def make_world(tmp_path, step: float = 0.0) -> SimpleNamespace:
    (tmp_path / "config.toml").write_text(render(VALID), encoding="utf-8")
    folder = tmp_path / "strategies"
    folder.mkdir(exist_ok=True)
    write(folder, "peeking", PEEKING)
    store, minute, clock = synthetic_store(tmp_path), MinuteStore(tmp_path), Clock(step)
    plugins = PluginFolder(folder)
    jobs = Jobs(store, minute, Keys("", "", ""), TAXES, lambda: plugins.plugins, InlinePool, 1, clock=clock.clock)
    services = Services(load_config(tmp_path / "config.toml"), store, minute, jobs, plugins, clock.clock)
    return SimpleNamespace(store=store, minute=minute, clock=clock, jobs=jobs, plugins=plugins, services=services)


def serve(world, scenario):
    async def go():
        async with TestClient(TestServer(make_app(world.services))) as client:
            assert (await client.post("/api/login", json={"token": TOKEN})).status == 204
            return await scenario(client)

    return asyncio.run(go())


async def answer(response) -> tuple[int, object]:
    if response.content_type == "application/json":
        return response.status, await response.json()
    return response.status, await response.read()


async def finished(world) -> None:
    while world.jobs.running():
        await asyncio.sleep(0.01)


def saved(world, raw: dict) -> int:
    """A result run and saved directly, as a backtest job would."""
    config = validate_config(raw, world.plugins.plugins)
    data = load_run(world.store, world.minute, config)
    return save_result(world.store, run_backtest(config, data, TAXES, strategy_factory(config, world.plugins.plugins)), NOW)


def rounded(values) -> list:
    return [float(f"{value:.6g}") if np.isfinite(value) else None for value in np.asarray(values, dtype=float).tolist()]


def put_minutes(world) -> list[int]:
    """1-minute bars of AAA and BBB over the last 6 sessions."""
    days = [int(day) for day in world.store.daily_bars("US", "SPY").day[-6:]]
    for k, day in enumerate(days):
        for symbol, base in (("AAA", 100.0), ("BBB", 50.0)):
            bars = minutes_of("US", day, range(0, 390), base + k + 0.01 * np.arange(390))
            world.minute.save_minute_day("US", symbol, MinuteCoverage(day, "OK", 1.0, NOW), bars)
    return days


# ----- backtests -----


def test_a_backtest_runs_through_the_api_and_every_view_of_it_is_served(world):
    async def scenario(client):
        started = await answer(await client.post("/api/backtests", json={"config": CONFIG}))
        await finished(world)
        world.clock.now += 60
        return {
            "started": started,
            "list": await answer(await client.get("/api/backtests?market=US&limit=10")),
            "detail": await answer(await client.get("/api/backtests/1")),
            "series": await answer(await client.get("/api/backtests/1/series")),
            "trades": await answer(await client.get("/api/backtests/1/trades?offset=1&limit=2")),
        }

    found = serve(world, scenario)
    assert found["started"] == (202, {"job": {"lane": "COMPUTE", "kind": "BACKTEST"}})
    (summary,) = found["list"][1]
    assert summary["strategy"] == {"source": "builtin", "id": "MA_CROSS"}
    assert summary["period"] == {"start": "2019-06-03", "end": None}
    assert set(summary["metrics"]) == {"total_return", "cagr", "sharpe", "max_drawdown", "trades"}
    assert summary["flags"] == ["SURVIVORSHIP"] and summary["keep_offline"] is False
    status, detail = found["detail"]
    row = world.store.backtest(1)
    assert status == 200 and detail["opened_at"] == "2026-10-28T01:01:00Z" and row.opened_at is not None
    assert detail["config"] == row.config and detail["time_unit"] == "day" and detail["flags"] == {"SURVIVORSHIP": None}
    assert detail["metrics"]["strategy"]["sharpe"] == float(f"{row.metrics['strategy']['sharpe']:.6g}")
    stored = world.store.backtest_series(1)
    status, series = found["series"]
    assert series["time_unit"] == "day" and series["t"] == stored["TIME"].astype(int).tolist()
    assert set(series["series"]) == {"EQUITY", "BENCHMARK", "CASH", "GROSS", "NET", "POSITIONS", "DRAWDOWN"}
    assert series["series"]["EQUITY"] == rounded(stored["EQUITY"])
    status, trades = found["trades"]
    every = world.store.backtest_trades(1)
    assert (trades["total"], trades["offset"], len(trades["trades"])) == (len(every), 1, 2)
    assert trades["trades"][0]["entry_time"] == every[1].entry_time
    assert trades["trades"][0]["pnl"] == float(f"{every[1].pnl:.6g}")


def test_backtest_errors(world, monkeypatch):
    async def scenario(client):
        found = [
            await answer(await client.post("/api/backtests", json={"config": {**CONFIG, "start": "soon"}})),
            await answer(await client.get("/api/backtests?market=JP")),
            await answer(await client.get("/api/backtests?limit=0")),
            await answer(await client.get("/api/backtests?limit=many")),
        ]
        for path in ("/api/backtests/9", "/api/backtests/9/series", "/api/backtests/9/trades",
                     "/api/backtests/9/candles/AAA", "/api/backtests/9/export/trades.csv", "/api/backtests/9/export/equity.csv"):
            found.append(await answer(await client.get(path)))
        found.append(await answer(await client.patch("/api/backtests/9", json={"name": "x"})))
        found.append(await answer(await client.delete("/api/backtests/9")))

        def busy(raw):
            raise JobRunning("COMPUTE")

        monkeypatch.setattr(world.jobs, "backtest", busy)
        found.append(await answer(await client.post("/api/backtests", json={"config": CONFIG})))
        return found

    found = serve(world, scenario)
    status, body = found[0]
    assert (status, body["error"], body["path"]) == (400, "INVALID_FIELD", "config.start")
    assert body["errors"] == [{"path": "config.start", "message": 'must be a date "YYYY-MM-DD"'}]
    assert [(status, body["path"]) for status, body in found[1:4]] == [(400, "market"), (400, "limit"), (400, "limit")]
    assert [(status, body["error"]) for status, body in found[4:12]] == [(404, "NOT_FOUND")] * 8
    assert (found[12][0], found[12][1]["error"]) == (409, "JOB_RUNNING")


def test_rename_keep_offline_and_delete_with_in_use(world):
    backtest_id = saved(world, CONFIG)
    other = saved(world, CONFIG)
    world.store._db.execute(
        "INSERT INTO live_portfolios (market, mode, backtest_id, config, capital, created_at, first_day)"
        " VALUES ('US', 'test', ?, '{}', 1000, '2026-10-28T01:00:00Z', 0)", (other,))

    async def scenario(client):
        return [
            await answer(await client.patch(f"/api/backtests/{backtest_id}", json={"name": "renamed", "keep_offline": True})),
            await answer(await client.patch(f"/api/backtests/{backtest_id}", json={"keep_offline": "yes"})),
            await answer(await client.patch(f"/api/backtests/{backtest_id}", json={"name": ""})),
            await answer(await client.delete(f"/api/backtests/{other}")),
            await answer(await client.delete(f"/api/backtests/{backtest_id}")),
            await answer(await client.get(f"/api/backtests/{backtest_id}/series")),
        ]

    steps = serve(world, scenario)
    assert steps[0] == (204, b"")
    assert [(status, body["error"], body.get("path")) for status, body in steps[1:4]] == [
        (400, "INVALID_FIELD", "keep_offline"), (400, "INVALID_FIELD", "name"), (409, "IN_USE", None)]
    assert steps[4] == (204, b"") and steps[5][0] == 404
    assert world.store.backtest_trades(backtest_id) == [] and world.store.backtest(other) is not None


def test_the_csv_exports(world):
    backtest_id = saved(world, CONFIG)

    async def scenario(client):
        found = []
        for name in ("trades", "equity"):
            response = await client.get(f"/api/backtests/{backtest_id}/export/{name}.csv")
            found.append((response.status, response.headers["Content-Type"], response.headers["Content-Disposition"],
                          await response.read()))
        return found

    (trades, equity) = serve(world, scenario)
    for status, content_type, disposition, body in (trades, equity):
        assert status == 200 and content_type == "text/csv; charset=utf-8" and body.startswith(b"\xef\xbb\xbf")
        assert disposition.startswith("attachment; filename=")
    assert trades[3][3:].split(b"\r\n")[0] == b"symbol,side,quantity,entry time,entry price,exit time,exit price,P&L,return %,costs,exit reason"
    assert equity[3][3:].split(b"\r\n")[0] == b"time,equity,benchmark,drawdown,gross exposure"
    assert len(trades[3].split(b"\r\n")) == len(world.store.backtest_trades(backtest_id)) + 2


def test_the_offline_set_is_the_20_most_recently_opened_plus_kept_ones(world):
    ids = [saved(world, CONFIG) for _ in range(3)]
    for extra in range(22):
        world.store.save_backtest(NOW, f"copy {extra}", "US", "1d", {"strategy": {"source": "builtin"}}, {}, {"strategy": {}},
                                  {}, {}, {"TIME": np.zeros(1)}, [])
    world.store.update_backtest(1, keep_offline=True)
    world.store.update_backtest(25, keep_offline=True)

    async def scenario(client):
        opened = []
        for backtest_id in range(1, 23):
            world.clock.now += 10
            opened.append((await answer(await client.get(f"/api/backtests/{backtest_id}")))[1]["opened_at"])
        return opened, await answer(await client.get("/api/offline"))

    opened, (status, found) = serve(world, scenario)
    assert opened[0] == "2026-10-28T01:00:10Z" and opened[-1] == "2026-10-28T01:03:40Z" and ids == [1, 2, 3]
    assert found == {"ids": [*range(22, 2, -1), 1, 25]}


# ----- candles -----


def test_candles_of_a_stored_symbol_adjusted_or_raw(world):
    days = WEEKDAYS[:-1]

    async def scenario(client):
        return [
            await answer(await client.get("/api/candles/US/AAA")),
            await answer(await client.get("/api/candles/US/AAA?adjusted=false")),
            await answer(await client.get(f"/api/candles/US/AAA?from={days[10]}&to={days[19]}")),
            await answer(await client.get("/api/candles/US/AAA?tf=2h")),
            await answer(await client.get("/api/candles/US/AAA?adjusted=maybe")),
            await answer(await client.get("/api/candles/US/AAA?from=soon")),
            await answer(await client.get("/api/candles/US/ZZZ")),
        ]

    steps = serve(world, scenario)
    bars = world.store.daily_bars("US", "AAA")
    expected = adjusted(bars, day_events("US", bars, world.store.actions("US", "AAA"))[0])
    (status, found), (_, raw) = steps[0], steps[1]
    assert status == 200 and found["time_unit"] == "day" and found["markers"] == []
    assert found["t"] == bars.day.tolist() and found["close"] == rounded(expected.close)
    assert raw["close"] == rounded(bars.close) and raw["close"][0] != found["close"][0]
    assert raw["close"][-1] == found["close"][-1]  # adjusted prices end at the raw ones (§6.3)
    assert steps[2][1]["t"] == days[10:20].tolist()
    assert [(status, body.get("path")) for status, body in steps[3:7]] == [
        (400, "tf"), (400, "adjusted"), (400, "from"), (404, None)]


def test_candles_come_in_windows_of_at_most_5000_bars_with_null_for_no_trade_days(world):
    days = np.arange(to_day(date(1990, 1, 1)), to_day(date(1990, 1, 1)) + 6_000)
    close = np.linspace(10.0, 20.0, len(days))
    put_bars(world.store, "US", "LONG", days, close)
    world.store.add_symbol("TW", "2454", "STOCK", NOW)
    world.store.upsert_daily_bars("TW", "2454", DailyBars(
        days[:3], np.array([600.0, np.nan, 602.0]), np.array([601.0, np.nan, 603.0]), np.array([599.0, np.nan, 601.0]),
        np.array([600.123456789, np.nan, 602.0]), np.array([1e3, 5.0, 1e3]), np.array([False, True, False])))

    async def scenario(client):
        return (await answer(await client.get("/api/candles/US/LONG?adjusted=false")),
                await answer(await client.get("/api/candles/TW/2454?adjusted=false")))

    (_, long), (_, tw) = serve(world, scenario)
    assert len(long["t"]) == 5_000 and long["t"][0] == int(days[1_000]) and long["t"][-1] == int(days[-1])
    assert tw["close"] == [600.123, None, 602.0] and tw["volume"] == [1000.0, 5.0, 1000.0]


def test_minute_candles_are_resampled_per_session_and_adjusted_by_their_day(world):
    day = int(WEEKDAYS[350])  # after AAA's dividend and before its 2-for-1 split: adjusted prices are half the raw ones
    world.minute.save_minute_day("US", "AAA", MinuteCoverage(day, "OK", 1.0, NOW),
                                 minutes_of("US", day, range(30), 100.0 + np.arange(30)))
    first, last = int(minutes_of("US", day, [0], [1.0]).t[0]), int(minutes_of("US", day, [29], [1.0]).t[0])

    async def scenario(client):
        return (await answer(await client.get(f"/api/candles/US/AAA?tf=5m&from={first}&to={last}&adjusted=false")),
                await answer(await client.get(f"/api/candles/US/AAA?tf=5m&from={first}&to={last}")))

    (_, raw), (_, scaled) = serve(world, scenario)
    assert raw["time_unit"] == "second" and raw["t"] == [first + 300 * k for k in range(6)]
    assert raw["close"] == [104.0, 109.0, 114.0, 119.0, 124.0, 129.0] and raw["volume"] == [500.0] * 6
    assert scaled["close"] == [52.0, 54.5, 57.0, 59.5, 62.0, 64.5] and scaled["volume"] == [1000.0] * 6


def test_backtest_candles_carry_the_trades_as_markers(world):
    backtest_id = saved(world, CONFIG)

    async def scenario(client):
        return (await answer(await client.get(f"/api/backtests/{backtest_id}/candles/AAA")),
                await answer(await client.get(f"/api/backtests/{backtest_id}/candles/SPY")))

    (status, found), (missing, _) = serve(world, scenario)
    trades = [t for t in world.store.backtest_trades(backtest_id) if t.symbol == "AAA"]
    expected = len(trades) + sum(t.exit_reason != "OPEN" for t in trades)
    assert status == 200 and len(found["markers"]) == expected > 0 and missing == 404
    assert found["close"] == rounded(world.store.daily_bars("US", "AAA").close)  # REALISTIC: the raw prices
    entry = found["markers"][0]
    assert (entry["side"], entry["reason"], entry["t"]) == ("BUY", "ENTRY", trades[0].entry_time)
    assert {marker["reason"] for marker in found["markers"]} <= {"ENTRY", "SIGNAL"}


# ----- compare -----


def test_compare_aligns_2_to_4_results_rescaled_to_1(world):
    days = put_minutes(world)
    daily = [saved(world, {**CONFIG, "start": "2020-06-01", "strategy": {"source": "builtin", "id": strategy}})
             for strategy in ("BUY_HOLD", "MA_CROSS", "RSI")]
    minute = saved(world, minute_config(days))

    async def scenario(client):
        return [await answer(await client.get(f"/api/compare?ids={ids}"))
                for ids in (f"{daily[0]},{minute}", f"{daily[0]},{daily[1]},{daily[2]},{minute}", f"{daily[0]}",
                            "1,2,3,4,5", "1,x", f"{daily[0]},99")]

    steps = serve(world, scenario)
    status, two = steps[0]
    assert status == 200 and two["time_unit"] == "day"
    first, second = two["series"][str(daily[0])], two["series"][str(minute)]
    assert first[0] == 1.0 and second[two["t"].index(days[3])] == 1.0  # the minute run starts on its own day
    assert second[0] is None and len(first) == len(second) == len(two["t"])
    assert [run["id"] for run in two["runs"]] == [daily[0], minute] and "sharpe" in two["runs"][0]["metrics"]
    assert steps[1][0] == 200 and len(steps[1][1]["series"]) == 4
    assert [(status, body["error"]) for status, body in steps[2:]] == [
        (400, "INVALID_FIELD"), (400, "INVALID_FIELD"), (400, "INVALID_FIELD"), (404, "NOT_FOUND")]


# ----- AC-30: every flag arises, is stored and is served -----


def flag_scenarios(world) -> dict[str, int]:
    days = put_minutes(world)
    short_days = WEEKDAYS[:-20]
    put_bars(world.store, "US", "ENDS", short_days, np.linspace(20.0, 30.0, len(short_days)))
    late = WEEKDAYS[300:]
    put_bars(world.store, "US", "LATE", late, np.linspace(40.0, 50.0, len(late)))
    first = to_date(days[0]).isoformat()
    return {
        "SURVIVORSHIP": saved(world, CONFIG),
        "SHORTS_IGNORED": saved(world, {**CONFIG, "strategy": {"source": "builtin", "id": "MA_CROSS",
                                                               "params": {"fast": 10, "slow": 30, "allow_short": True}}}),
        "PATH_FALLBACK": saved(world, {**CONFIG, "intrabar_path": "MINUTE", "exits": {"stop_loss_pct": 1.0}}),
        "DATA_ENDED": saved(world, {**CONFIG, "symbols": ["AAA", "ENDS"], "strategy": {"source": "builtin", "id": "BUY_HOLD"}}),
        "BENCHMARK_PARTIAL": saved(world, {**CONFIG, "benchmark": "LATE"}),
        "PDT": saved(world, minute_config(days, timeframe="1m", initial_capital=10_000.0, start=first)),
    }


def test_every_flag_arises_is_stored_and_is_served(world):
    scenarios = flag_scenarios(world)

    async def scenario(client):
        await client.post("/api/backtests", json={"config": {**CONFIG, "strategy": {"source": "plugin", "id": "peeking"}}})
        await finished(world)
        scenarios["LOOKAHEAD_SUSPECT"] = world.store.backtests(limit=1)[0].id
        found = {flag: (await answer(await client.get(f"/api/backtests/{backtest_id}")))[1]["flags"]
                 for flag, backtest_id in scenarios.items()}
        return found, (await answer(await client.get("/api/backtests?limit=1000")))[1]

    found, listed = serve(world, scenario)
    for flag, flags in found.items():
        assert flag in flags and "SURVIVORSHIP" in flags, (flag, flags)
        assert flags == world.store.backtest(scenarios[flag]).flags
    assert isinstance(found["PATH_FALLBACK"]["PATH_FALLBACK"], int) and found["PDT"]["PDT"] >= 1
    assert set(found["LOOKAHEAD_SUSPECT"]["LOOKAHEAD_SUSPECT"]) == {"time", "symbol"}
    assert {flag for summary in listed for flag in summary["flags"]} == {
        "SURVIVORSHIP", "SHORTS_IGNORED", "PATH_FALLBACK", "DATA_ENDED", "BENCHMARK_PARTIAL", "PDT", "LOOKAHEAD_SUSPECT"}


# ----- optimizations -----

REQUEST = {"config": CONFIG, "min_trades": 0,
           "ranges": {"strategy.params.fast": {"values": [5, 10]}, "strategy.params.slow": {"values": [20, 40, 60]}}}


def test_an_optimization_runs_and_is_listed_shown_mapped_promoted_and_deleted(world):
    async def scenario(client):
        steps = {"started": await answer(await client.post("/api/optimizations", json=REQUEST))}
        await finished(world)
        steps["list"] = await answer(await client.get("/api/optimizations"))
        steps["detail"] = await answer(await client.get("/api/optimizations/1"))
        steps["row"] = world.store.optimization(1)
        steps["heatmap"] = await answer(await client.get(
            "/api/optimizations/1/heatmap?x=strategy.params.fast&y=strategy.params.slow"))
        steps["bad_heatmap"] = await answer(await client.get("/api/optimizations/1/heatmap?x=strategy.params.fast&y=name"))
        steps["promoted"] = await answer(await client.post("/api/optimizations/1/promote", json={"combo": [10, 40]}))
        await finished(world)
        steps["bad_combo"] = await answer(await client.post("/api/optimizations/1/promote", json={"combo": [10]}))
        steps["invalid_combo"] = await answer(await client.post("/api/optimizations/1/promote", json={"combo": [60, 20]}))
        steps["deleted"] = await answer(await client.delete("/api/optimizations/1"))
        steps["gone"] = [await answer(await client.request(method, path, json={"combo": [5, 20]}))
                         for method, path in (("GET", "/api/optimizations/1"), ("GET", "/api/optimizations/1/heatmap"),
                                              ("POST", "/api/optimizations/1/promote"), ("DELETE", "/api/optimizations/1"))]
        return steps

    steps = serve(world, scenario)
    assert steps["started"] == (202, {"job": {"lane": "COMPUTE", "kind": "OPTIMIZE"}})
    (summary,) = steps["list"][1]
    assert (summary["mode"], summary["status"], summary["name"]) == ("GRID", "DONE", "trend") and "results" not in summary
    detail, row = steps["detail"][1], steps["row"]
    assert detail["config"] == row.config and detail["results"]["paths"] == row.results["paths"]
    stored = row.results["combinations"][0]
    assert detail["results"]["combinations"][0]["values"] == [5, 20]
    assert detail["results"]["combinations"][0]["metrics"]["sharpe"] == float(f"{stored['metrics']['sharpe']:.6g}")
    heat = steps["heatmap"][1]
    assert heat["x"]["values"] == [5, 10] and heat["y"]["values"] == [20, 40, 60] and len(heat["cells"]) == 3
    assert (steps["bad_heatmap"][0], steps["bad_heatmap"][1]["path"]) == (400, "x")
    assert steps["promoted"] == (202, {"job": {"lane": "COMPUTE", "kind": "BACKTEST"}})
    (promoted,) = world.store.backtests(limit=1)
    assert promoted.name == "trend (strategy.params.fast 10, strategy.params.slow 40)"
    assert promoted.config["strategy"]["params"] == {"fast": 10, "slow": 40, "ma": "SMA", "allow_short": False}
    assert [(status, body["path"]) for status, body in (steps["bad_combo"], steps["invalid_combo"])] == [
        (400, "combo"), (400, "combo")]
    assert steps["deleted"] == (204, b"")
    assert [status for status, _ in steps["gone"]] == [404] * 4


def test_optimization_requests_that_cannot_start(world, monkeypatch):
    async def scenario(client):
        found = [
            await answer(await client.post("/api/optimizations", json={**REQUEST, "objective": "MAX"})),
            await answer(await client.post("/api/optimizations", json={
                **REQUEST, "ranges": {"initial_capital": {"min": 1, "max": 3000, "step": 1}}})),
        ]

        def busy(raw):
            raise JobRunning("COMPUTE")

        monkeypatch.setattr(world.jobs, "optimization", busy)
        found.append(await answer(await client.post("/api/optimizations", json=REQUEST)))
        return found

    steps = serve(world, scenario)
    assert [(status, body["error"], body.get("path")) for status, body in steps] == [
        (400, "INVALID_FIELD", "objective"), (400, "TOO_MANY_COMBOS", None), (409, "JOB_RUNNING", None)]


def test_a_long_optimization_needs_confirmation(tmp_path):
    world = make_world(tmp_path, step=1_000.0)  # each clock reading moves 1,000 s: the estimate is 6 × 1,000 s
    try:
        async def scenario(client):
            first = await answer(await client.post("/api/optimizations", json=REQUEST))
            await finished(world)
            second = await answer(await client.post("/api/optimizations", json={**REQUEST, "confirm_long": True}))
            await finished(world)
            return first, second, world.store.optimization(1).status

        (status, body), confirmed, saved_status = serve(world, scenario)
    finally:
        world.store.close()
        world.minute.close()
    assert (status, body["error"], body["estimate_seconds"]) == (409, "NEEDS_CONFIRMATION", 6_000.0)
    assert confirmed[0] == 202 and saved_status == "DONE"
