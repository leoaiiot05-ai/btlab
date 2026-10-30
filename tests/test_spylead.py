"""spylead: the client against a fake of its API, the snapshot job, alignment by calendar day and IN_SAMPLE_SERIES
(SPEC.md §5.6, §8.5, §10.11, §17.2, AC-23).

spylead is an aiohttp test server on 127.0.0.1, or a scripted session inside a data update; nothing waits.
"""

import asyncio
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import aiohttp
import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from numpy.testing import assert_array_equal

from btlab.backtest import RunConfigError, load_run, minute_view, run_backtest, stored_series, strategy_factory, validate_config
from btlab.config import ServiceConfig
from btlab.http import Requester, SourceError
from btlab.intraday import minute_timeline
from btlab.jobs import Jobs, KeepAwake
from btlab.optimize import optimize, parse_request
from btlab.plugins import Bars, load_plugin
from btlab.records import ExternalSeries, ExternalSnapshot, to_date, to_day
from btlab.rules import directions
from btlab.spylead_client import SpyleadClient
from btlab.store import Store
from builders import TAXES, WEEKDAYS, synthetic_store
from fakes import Answer, FakeSpylead, InlinePool
from test_config import VALID
from test_intraday import minute_config, stores  # noqa: F401  (stores is a fixture)
from test_jobs import KEYS, NOW, Harness, us_answers
from test_plugins import write
from test_rules import daily_view

TOKEN = VALID["spylead"]["token"]
NAN = math.nan
JAN_1 = to_day(date(2026, 1, 1))
EARLIER = NOW - timedelta(days=1)
FIVE = ["SPYL1", "LEADL1_HYG", "LEADL1_TLT", "F", "COMPOSITE"]


async def no_sleep(seconds: float) -> None:
    return None


def snapshot_of(fake: FakeSpylead, token: str = TOKEN) -> tuple[ExternalSeries, ...]:
    """The client's snapshot from a test server of fake."""
    async def go():
        async with TestServer(fake.app()) as server, aiohttp.ClientSession() as session:
            client = SpyleadClient(str(server.make_url("/")), token, Requester("spylead", session, sleep=no_sleep))
            return await client.snapshot()

    return asyncio.run(go())


def failure(fake: FakeSpylead, token: str = TOKEN) -> SourceError:
    with pytest.raises(SourceError) as caught:
        snapshot_of(fake, token)
    return caught.value


# ----- the client (§5.6) -----


def test_the_client_logs_in_then_reads_the_state_spy_the_kept_series_and_f():
    fake = FakeSpylead(TOKEN)
    found = snapshot_of(fake)
    # Only the login goes without the session cookie; XLF is not KEPT.
    assert fake.requests == [("POST", "/api/login", False), ("GET", "/api/state", True), ("GET", "/api/charts/spy", True),
                             ("GET", "/api/charts/series/HYG", True), ("GET", "/api/charts/series/TLT", True),
                             ("GET", "/api/charts/f", True)]
    assert [(s.name, s.first_day, s.in_sample) for s in found] == [
        ("SPYL1", JAN_1, False), ("LEADL1_HYG", JAN_1 + 1, False), ("LEADL1_TLT", JAN_1 + 1, False),
        ("F", JAN_1, True), ("COMPOSITE", JAN_1, True)]
    assert_array_equal(found[0].values, [1.0, NAN, 3.0])
    assert_array_equal(found[4].values, [NAN, 1.5])
    assert all(s.values.dtype == np.float64 for s in found)


def test_f_and_composite_wait_until_spylead_says_f_is_ok():
    fake = FakeSpylead(TOKEN, statuses={"HYG": "DROPPED"}, f_status="FITTING")
    assert [s.name for s in snapshot_of(fake)] == ["SPYL1"]
    assert [path for _, path, _ in fake.requests] == ["/api/login", "/api/state", "/api/charts/spy"]


def test_a_refused_token_fails_without_showing_the_token_or_the_address():
    error = failure(FakeSpylead(TOKEN), token="Sentinel-Wrong-Token-7f3a")
    assert (error.code, error.text) == ("HTTP_401", "spylead answered 401: login")


def test_an_empty_token_fails_before_any_request():
    fake = FakeSpylead(TOKEN)
    assert failure(fake, token="").code == "MISSING_KEY" and fake.requests == []


@pytest.mark.parametrize(("path", "change", "text"), [
    ("/api/charts/f", lambda chart: chart["f"].update(values=[1.0, "x"]), "spylead's F: values is not a list of numbers"),
    ("/api/charts/spy", lambda chart: chart["rt"].update(first_day="soon"), "spylead's SPYL1: first_day is not a day"),
    ("/api/charts/spy", lambda chart: chart.pop("rt"), "spylead's SPY chart has no field rt"),
])
def test_a_payload_that_is_not_a_series_is_bad(path, change, text):
    fake = FakeSpylead(TOKEN)
    change(fake.charts[path])
    error = failure(fake)
    assert (error.code, error.text) == ("BAD_PAYLOAD", text)


# ----- the snapshot (§5.6, §17.2) -----


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path)
    opened.replace_external_series(ExternalSnapshot(EARLIER, (ExternalSeries("LEADL1_OLD", JAN_1, np.ones(3), False),)))
    yield opened
    opened.close()


def snapshot_job(store: Store, fake: FakeSpylead | None):
    """A SPYLEAD_SNAPSHOT job against a test server of fake, or against a server that has stopped."""
    async def go():
        server = TestServer(fake.app() if fake else web.Application())
        await server.start_server()
        url = str(server.make_url("/"))
        if fake is None:
            await server.close()
        jobs = Jobs(store, None, KEYS, TAXES, lambda: {}, InlinePool, 1, clock=lambda: NOW.timestamp(), sleep=no_sleep,
                    keep_awake=KeepAwake(lambda flags: None), spylead=ServiceConfig(url, TOKEN))
        job = jobs.snapshot()
        await job.task
        await server.close()
        return store.latest_job_runs("SPYLEAD_SNAPSHOT", 1)[0]

    return asyncio.run(go())


def test_a_snapshot_replaces_the_stored_set_in_one_transaction(store):
    run = snapshot_job(store, FakeSpylead(TOKEN))
    snapshot = store.external_snapshot()
    assert (run.lane, run.outcome, run.message) == ("DATA", "UPDATED", "series 5")
    assert snapshot.taken_at == NOW and sorted(s.name for s in snapshot.series) == sorted(FIVE)
    assert_array_equal(stored_series(store)["COMPOSITE"].values, [NAN, 1.5])


def test_a_snapshot_failing_halfway_keeps_the_previous_snapshot_and_records_why(store):
    fake = FakeSpylead(TOKEN)
    fake.failing.add("/api/charts/f")
    run = snapshot_job(store, fake)
    snapshot = store.external_snapshot()
    assert (snapshot.taken_at, [s.name for s in snapshot.series]) == (EARLIER, ["LEADL1_OLD"])
    assert run.outcome == "FAILED" and run.message == "HTTP_500: spylead answered 500: F"
    # The F chart was tried 4 times, after everything before it had answered.
    assert [path for _, path, _ in fake.requests].count("/api/charts/f") == 4 and len(fake.requests) == 9
    kinds = [(event.kind, event.message) for event in reversed(store.latest_events(10))]
    assert kinds == [("SPYLEAD_UNREACHABLE", run.message), ("JOB_FAILED", f"SPYLEAD_SNAPSHOT FAILED: {run.message}")]


def test_an_unreachable_spylead_keeps_the_previous_snapshot(store):
    run = snapshot_job(store, None)
    assert [s.name for s in store.external_snapshot().series] == ["LEADL1_OLD"]
    assert run.outcome == "FAILED" and run.message.startswith("UNREACHABLE: spylead cannot be reached")
    assert "127.0.0.1" not in run.message
    assert [event.kind for event in store.latest_events(10)] == ["JOB_FAILED", "SPYLEAD_UNREACHABLE"]


def spylead_answers() -> list[Answer]:
    return [Answer(204, b"", {"Set-Cookie": "spylead_session=c0ffee; HttpOnly; Path=/"}),
            Answer(200, {"series": [{"name": "HYG", "status": "KEPT"}], "f": {"status": "OK"}}),
            Answer(200, {"rt": {"first_day": "2026-01-01", "values": [1.0]}}),
            Answer(200, {"rt": {"first_day": "2026-01-01", "values": [2.0]}}),
            Answer(200, {"f": {"first_day": JAN_1, "values": [3.0]}, "composite": {"first_day": JAN_1, "values": [4.0]}})]


def updating(store: Store, *spylead: Answer) -> Harness:
    store.set_list_symbols(store.create_list("mine", "US", NOW), ["AAA"])
    harness = Harness(store, *us_answers(), *spylead, spylead=ServiceConfig("http://127.0.0.1:8090/", TOKEN))
    harness.run(lambda jobs: jobs.update("US"))
    return harness


def test_a_data_update_ends_with_a_snapshot_when_spylead_is_configured(store):
    harness = updating(store, *spylead_answers())
    login, state = harness.session.calls[3:5]
    assert (login.method, login.url, login.json) == ("POST", "http://127.0.0.1:8090/api/login", {"token": TOKEN})
    assert (state.method, state.url, state.headers) == ("GET", "http://127.0.0.1:8090/api/state", {"Cookie": "spylead_session=c0ffee"})
    assert sorted(s.name for s in store.external_snapshot().series) == ["COMPOSITE", "F", "LEADL1_HYG", "SPYL1"]
    assert harness.last("UPDATE_US").outcome == "UPDATED"


def test_spylead_failing_in_an_update_leaves_the_update_alone(store):
    harness = updating(store, Answer(401, {"error": "LOGIN_FAILED"}))
    assert (harness.last("UPDATE_US").outcome, harness.last("UPDATE_US").message) == ("UPDATED", "bars 4, actions 1, rates 2")
    assert [s.name for s in store.external_snapshot().series] == ["LEADL1_OLD"]
    assert harness.events() == [("UPDATE_STARTED", None), ("SPYLEAD_UNREACHABLE", None), ("UPDATE_FINISHED", None)]


# ----- alignment by calendar day (§5.6, §8.5) -----


def test_a_bar_takes_its_calendar_days_value_and_nan_outside_the_series():
    days = np.array([JAN_1 - 1, JAN_1, JAN_1 + 1, JAN_1 + 4, JAN_1 + 5], dtype=np.int64)
    found = ExternalSeries("F", JAN_1, np.array([10.0, 11.0, NAN, 12.0, 13.0]), True)
    view = replace(daily_view(np.ones(5), days=days), external={"F": found})
    assert view.read == set()
    assert_array_equal(view.series("F"), [NAN, 10.0, 11.0, 13.0, NAN])
    assert view.read == {"F"}


def test_minute_bars_take_their_sessions_calendar_day(stores):
    daily, minute, days = stores
    values = np.arange(days[-1] - days[0] + 1, dtype=np.float64)
    daily.replace_external_series(ExternalSnapshot(NOW, (ExternalSeries("SPYL1", days[0], values, False),)))
    config = validate_config(minute_config(days))
    data = load_run(daily, minute, config)
    run = minute_timeline("US", "15m", data.aligned, data.minutes, data.covered, config.start)
    bars = Bars(minute_view(run, data))
    assert len(set(run.timeline.day.tolist())) > 1
    assert_array_equal(bars.ext("SPYL1"), run.timeline.day - days[0])
    assert not bars.ext("SPYL1").flags.writeable
    with pytest.raises(KeyError, match="no external series F"):
        bars.ext("F")


def series_rule(*names: str, level: object = 0) -> dict:
    """Long while every named series is above level."""
    above = [{"left": {"series": name}, "op": "gt", "right": {"value": level}} for name in names]
    return {"version": 1, "timeframe": "1d", "side": "long",
            "params": {"level": {"type": "float", "default": 0.0, "min": -1.0, "max": 1.0, "step": 0.1}},
            "entry": {"all": above}, "exit": {"not": {"all": above}}}


def wave(name: str, in_sample: bool, days: np.ndarray = WEEKDAYS) -> ExternalSeries:
    return ExternalSeries(name, int(days[0]), np.sin(np.arange(int(days[-1] - days[0]) + 1) / 9.0), in_sample)


def test_a_rule_on_a_series_reads_nothing_after_its_bar():
    """No look-ahead: on data cut after a bar, the directions up to it are the full run's."""
    days = WEEKDAYS[:80]
    external = {"F": wave("F", True, days)}
    full = directions(series_rule("F"), {}, replace(daily_view(np.ones(80), days=days), external=external))
    assert 0 < full.sum() < 80
    for count in (10, 41, 79):
        part = directions(series_rule("F"), {}, replace(daily_view(np.ones(count), days=days[:count]), external=external))
        assert_array_equal(part, full[:count])


# ----- IN_SAMPLE_SERIES (§5.6, §10.11) -----


EXTERNAL = '''
NAME_EN = "External"
NAME_ZH = "外部序列"
TIMEFRAME = "1d"
PARAMS = {"name": {"type": "choice", "default": "COMPOSITE", "choices": ["COMPOSITE", "SPYL1"]}}

import numpy as np

def signals(bars, params):
    level = bars.ext(params["name"])
    return np.where(level[:, None] > 0, 0.5, 0.0) * np.ones(bars.close.shape)
'''


@pytest.fixture
def market(tmp_path):
    found = synthetic_store(tmp_path)
    found.replace_external_series(ExternalSnapshot(NOW, (wave("SPYL1", False), wave("F", True), wave("COMPOSITE", True))))
    yield found
    found.close()


def config(strategy: dict) -> dict:
    return {"name": "spylead", "market": "US", "symbols": ["AAA", "BBB"], "start": to_date(int(WEEKDAYS[300])).isoformat(),
            "strategy": strategy}


def backtest(store: Store, strategy: dict, plugins=None):
    found = validate_config(config(strategy), plugins, stored_series(store))
    return run_backtest(found, load_run(store, None, found), TAXES, strategy_factory(found, plugins))


@pytest.mark.parametrize(("names", "flag"), [(("F",), ["F"]), (("F", "COMPOSITE"), ["COMPOSITE", "F"]), (("SPYL1",), None)])
def test_a_rule_that_reads_f_or_composite_is_flagged(market, names, flag):
    result = backtest(market, {"source": "rule", "rule": series_rule(*names)})
    assert result.flags.get("IN_SAMPLE_SERIES") == flag and result.metrics["strategy"]["trades"] > 0


@pytest.mark.parametrize(("name", "flag"), [("COMPOSITE", ["COMPOSITE"]), ("SPYL1", None)])
def test_a_plugin_that_reads_composite_through_bars_ext_is_flagged(market, tmp_path, name, flag):
    plugin = load_plugin(write(tmp_path, "external", EXTERNAL))
    result = backtest(market, {"source": "plugin", "id": "external", "params": {"name": name}}, {"external": plugin})
    assert result.flags.get("IN_SAMPLE_SERIES") == flag


def test_an_optimization_over_a_rule_on_f_is_flagged(market):
    request = parse_request({"config": config({"source": "rule", "rule": series_rule("F", level={"param": "level"})}),
                             "ranges": {"strategy.params.level": {"values": [0.0, 0.5]}}, "min_trades": 0},
                            None, stored_series(market))
    found = optimize(request, load_run(market, None, request.config), TAXES, InlinePool, 1)
    assert found.flags["IN_SAMPLE_SERIES"] == ["F"]


def test_a_series_must_be_stored_for_a_rule_to_read_it(market):
    with pytest.raises(RunConfigError) as caught:
        validate_config(config({"source": "rule", "rule": series_rule("LEADL1_HYG")}), None, stored_series(market))
    assert caught.value.errors[0][0] == "strategy.rule.entry.all[0].left.series"
    assert caught.value.errors[0][1].startswith("UNKNOWN_SERIES")


def test_a_backtest_job_reads_the_stored_snapshot(market):
    async def go():
        jobs = Jobs(market, None, KEYS, TAXES, lambda: {}, InlinePool, 1, clock=lambda: NOW.timestamp(), sleep=no_sleep,
                    keep_awake=KeepAwake(lambda flags: None))
        job = jobs.backtest(config({"source": "rule", "rule": series_rule("COMPOSITE")}))
        await job.task

    asyncio.run(go())
    (summary,) = market.backtests("US", 1)
    assert summary.flags["IN_SAMPLE_SERIES"] == ["COMPOSITE"]
