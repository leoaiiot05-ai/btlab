"""Jobs: lanes, data updates, backtest and optimization jobs, and the schedule (SPEC.md §17, AC-18).

Sources are HTTP fakes, stores are temporary, the clock and the sleep are injected and the pool runs in process.
"""

import asyncio
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.backtest import RunConfigError, load_run, run_backtest, strategy_factory, validate_config
from btlab.config import ScheduleConfig, ServiceConfig
from btlab.jobs import JobRunning, Jobs, KeepAwake, Keys
from btlab.optimize import NeedsConfirmation
from btlab.records import to_day
from btlab.store import Store
from builders import TAXES, synthetic_store
from fakes import Answer, FakeSession, InlinePool
from test_finmind import ok
from test_fred import observation
from test_tiingo import row as tiingo_row

KEYS = Keys("Sentinel-Tiingo-Key-5e2d", "Sentinel-FinMind-Token-9d1a", "Sentinel-Fred-Key-0c4b")
TAIPEI = ZoneInfo("Asia/Taipei")
# Wednesday 28 October 2026, 09:00 in Taipei, still the 27th in New York.
NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)
ON, OFF = 0x80000001, 0x80000000
SPY = [tiingo_row("2026-10-26", 580.0), tiingo_row("2026-10-27", 582.0)]
AAA = [tiingo_row("2026-10-26", 50.0), tiingo_row("2026-10-27", 51.0, div=0.25)]
RATES = {"observations": [observation("2026-10-26", "3.90"), observation("2026-10-27", "3.92")]}


class Clock:
    """The injected clock, and a sleep that yields to the event loop after running an optional hook."""

    def __init__(self, start: datetime = NOW, step: float = 0.0) -> None:
        self.now, self.step = start.timestamp(), step
        self.sleeps: list[float] = []
        self.hook = None

    def clock(self) -> float:
        self.now += self.step
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.hook is not None:
            self.hook()
        self.now += seconds
        await asyncio.sleep(0)


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, kind: str, **fields: str) -> None:
        self.sent.append((kind, fields))


class Harness:
    def __init__(self, store: Store, *answers: Answer, keys: Keys = KEYS, start: datetime = NOW, step: float = 0.0,
                 pool=InlinePool, session=None, spylead: ServiceConfig = ServiceConfig("", "")) -> None:
        self.store, self.session, self.time = store, FakeSession(*answers), Clock(start, step)
        self.states: list[int] = []
        self.notes = Notes()
        self.jobs = Jobs(store, None, keys, TAXES, lambda: {}, pool, 1, notifier=self.notes,
                         session=session or (lambda: self.session), clock=self.time.clock, sleep=self.time.sleep,
                         keep_awake=KeepAwake(self.states.append), spylead=spylead)

    def run(self, start):
        """Starts a job and waits for its end."""
        async def go():
            job = start(self.jobs)
            await job.task
            return job

        return asyncio.run(go())

    def last(self, kind: str):
        return self.store.latest_job_runs(kind, 1)[0]

    def events(self) -> list[tuple[str, str | None]]:
        return [(event.kind, event.symbol) for event in reversed(self.store.latest_events(100))]


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path)
    for market, symbols in (("US", ["AAA"]), ("TW", ["2330"])):
        opened.set_list_symbols(opened.create_list(f"mine {market}", market, NOW), symbols)
    yield opened
    opened.close()


def us_answers(spy=SPY, aaa=AAA) -> list[Answer]:
    return [Answer(200, spy), Answer(200, aaa), Answer(200, RATES)]


def full_budget(store: Store, count: int) -> None:
    """count Tiingo requests in the hour before NOW, the oldest 50 minutes ago."""
    store.save_request_log("tiingo", [NOW.timestamp() - 3000.0 + k for k in range(count)])


async def forever(seconds: float) -> None:
    await asyncio.Event().wait()


# ----- US updates (§17.2, §5.1) -----


def test_an_update_stores_new_rows_then_revised_ones_then_nothing(store):
    first = Harness(store, *us_answers())
    first.run(lambda jobs: jobs.update("US"))
    run = first.last("UPDATE_US")
    assert (run.lane, run.outcome, run.message, run.scheduled_for) == ("DATA", "UPDATED", "bars 4, actions 1, rates 2", None)
    tiingo_spy, tiingo_aaa, fred = first.session.calls
    assert (tiingo_spy.params["startDate"], tiingo_spy.params["endDate"]) == ("1995-10-27", "2026-10-27")
    assert tiingo_aaa.url.endswith("/AAA/prices") and fred.params["observation_start"] == "1995-10-27"
    assert store.data_start("US") == to_day(date(1995, 10, 27))
    assert [(s.symbol, s.category, s.last_fetch_error) for s in store.symbols("US")] == [("AAA", "STOCK", None), ("SPY", "STOCK", None)]
    assert len(store.request_log("tiingo")) == 2
    assert first.events() == [("UPDATE_STARTED", None), ("UPDATE_FINISHED", None)]
    assert first.states == [ON, OFF]
    # A day later Tiingo revises the 27th: the 7-day overlap upserts it, and DTB3 is asked from 30 days back.
    spy = [SPY[0], tiingo_row("2026-10-27", 583.0)]
    revised = Harness(store, *us_answers(spy=spy), start=NOW + timedelta(days=1))
    revised.run(lambda jobs: jobs.update("US"))
    assert (revised.last("UPDATE_US").outcome, revised.last("UPDATE_US").message) == ("UPDATED", "bars 1")
    assert revised.session.calls[0].params["startDate"] == "2026-10-20"
    assert revised.session.calls[2].params["observation_start"] == "2026-09-27"
    assert store.daily_bars("US", "SPY").close.tolist() == [580.0, 583.0]
    again = Harness(store, *us_answers(spy=spy), start=NOW + timedelta(days=1))
    again.run(lambda jobs: jobs.update("US"))
    assert (again.last("UPDATE_US").outcome, again.last("UPDATE_US").message) == ("UP_TO_DATE", "nothing new")
    assert again.events()[-1] == ("UP_TO_DATE", None)


def test_a_failing_symbol_keeps_its_error_and_the_update_is_partial(store):
    harness = Harness(store, Answer(200, SPY), Answer(404, {"detail": "Not found."}), Answer(200, RATES))
    harness.run(lambda jobs: jobs.update("US"))
    run = harness.last("UPDATE_US")
    assert (run.outcome, run.message) == ("PARTIAL", "failed: AAA; bars 2, rates 2")
    errors = {s.symbol: s.last_fetch_error for s in store.symbols("US")}
    assert errors["SPY"] is None and errors["AAA"].startswith("SYMBOL_UNKNOWN:")
    assert ("SYMBOL_FAILED", "AAA") in harness.events() and len(store.daily_bars("US", "SPY").day) == 2


def test_an_update_where_every_symbol_fails_has_failed(store):
    harness = Harness(store, Answer(404), Answer(404), Answer(200, RATES))
    harness.run(lambda jobs: jobs.update("US"))
    assert (harness.last("UPDATE_US").outcome, harness.last("UPDATE_US").message) == ("FAILED", "every symbol failed: SPY, AAA")
    assert harness.events()[-1] == ("JOB_FAILED", None)


def test_a_missing_key_fails_the_update_without_a_request(store):
    harness = Harness(store, keys=Keys("", KEYS.finmind, KEYS.fred))
    harness.run(lambda jobs: jobs.update("US"))
    run = harness.last("UPDATE_US")
    assert (run.outcome, run.message) == ("FAILED", "MISSING_KEY: the Tiingo API key is empty")
    assert harness.session.calls == [] and harness.events() == [("JOB_FAILED", None)]


def test_a_missing_fred_key_leaves_the_bars_and_makes_the_update_partial(store):
    harness = Harness(store, *us_answers()[:2], keys=Keys(KEYS.tiingo, KEYS.finmind, ""))
    harness.run(lambda jobs: jobs.update("US"))
    assert harness.last("UPDATE_US").message == "failed: DTB3; bars 4, actions 1"
    assert ("SYMBOL_FAILED", "DTB3") in harness.events()


def test_the_update_waits_for_tiingos_budget_and_says_until_when(store):
    full_budget(store, 44)  # SPY takes the hour's 45th request; AAA waits until the oldest ages out
    harness = Harness(store, *us_answers())
    seen = []
    harness.time.hook = lambda: seen.append(harness.jobs.running()["DATA"])
    harness.run(lambda jobs: jobs.update("US"))
    assert harness.time.sleeps == [600.0]
    assert {key: seen[0][key] for key in ("kind", "phase", "done", "total", "item", "message")} == {
        "kind": "UPDATE_US", "phase": "BARS", "done": 1, "total": 2, "item": "AAA",
        "message": "waiting for Tiingo's request limit until 09:10",
    }
    assert harness.last("UPDATE_US").outcome == "UPDATED" and len(store.request_log("tiingo")) == 46


def test_a_cancel_ends_the_wait_and_keeps_what_was_stored(store):
    full_budget(store, 44)
    harness = Harness(store, *us_answers())
    harness.time.hook = lambda: harness.jobs.cancel("DATA")
    harness.run(lambda jobs: jobs.update("US"))
    assert harness.last("UPDATE_US").outcome == "CANCELLED" and harness.events()[-1] == ("JOB_CANCELLED", None)
    assert len(store.daily_bars("US", "SPY").day) == 2 and len(store.daily_bars("US", "AAA").day) == 0
    assert harness.jobs.running() == {} and harness.states == [ON, OFF]


def test_a_shutdown_interrupts_a_waiting_update(store):
    full_budget(store, 45)
    harness = Harness(store)
    harness.jobs.sleep = forever

    async def go():
        job = harness.jobs.update("US")
        while job.message is None:
            await asyncio.sleep(0)
        await harness.jobs.interrupt()

    asyncio.run(go())
    assert harness.last("UPDATE_US").outcome == "INTERRUPTED" and harness.events()[-1] == ("JOB_INTERRUPTED", None)


def test_one_job_per_lane_while_other_lanes_run(market):
    full_budget(market, 45)
    harness = Harness(market)
    harness.jobs.sleep = forever
    config = {"name": "side", "market": "US", "symbols": ["AAA"], "start": "2020-01-02",
              "strategy": {"source": "builtin", "id": "BUY_HOLD"}}

    async def go():
        update = harness.jobs.update("US")
        with pytest.raises(JobRunning) as caught:
            harness.jobs.update("TW")
        assert caught.value.lane == "DATA"
        backtest = harness.jobs.backtest(config)
        assert set(harness.jobs.running()) == {"DATA", "COMPUTE"}
        await backtest.task
        assert harness.jobs.cancel("DATA") and not harness.jobs.cancel("COMPUTE")
        await update.task

    asyncio.run(go())
    assert harness.last("BACKTEST").outcome == "DONE" and harness.last("UPDATE_US").outcome == "CANCELLED"
    assert harness.states == [ON, OFF]  # held from the first job's start to the last one's end


def test_tiingos_monthly_symbols_warn_past_450(store):
    store.save_setting("tiingo_symbols/2026-10", [f"X{k}" for k in range(450)])
    harness = Harness(store, *us_answers())
    harness.run(lambda jobs: jobs.update("US"))
    assert [kind for kind, _ in harness.events()].count("TIINGO_SYMBOL_BUDGET") == 1
    assert len(store.setting("tiingo_symbols/2026-10")) == 452


def test_an_unexpected_error_fails_the_job_as_an_internal_error(store):
    def broken():
        raise RuntimeError("no session today")

    harness = Harness(store, session=broken)
    harness.run(lambda jobs: jobs.update("US"))
    assert harness.last("UPDATE_US").message == "RuntimeError: no session today"
    assert ("INTERNAL_ERROR", None) in harness.events()
    assert harness.notes.sent == [("INTERNAL_ERROR", {"error": "RuntimeError: no session today"})]


# ----- TW updates (§17.2, §5.2) -----

TW_NOW = datetime(2026, 10, 28, 18, 5, tzinfo=TAIPEI)


def finmind_price(iso: str, close: float) -> dict:
    return {"date": iso, "open": close, "max": close + 1, "min": close - 1, "close": close, "Trading_Volume": 1000}


def tw_answers(info: bool = True) -> list[Answer]:
    calendar = ok([{"date": "2026-10-26"}, {"date": "2026-10-27"}, {"date": "2026-10-28"}])
    names = ok([{"stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "industry_category": "ETF", "date": "2026-10-28"},
                {"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業", "date": "2026-10-28"}])
    split = ok([{"date": "2026-10-27", "stock_id": "0050", "type": "分割", "before_price": 200.0, "after_price": 50.0}])
    etf = [ok([finmind_price("2026-10-26", 200.0), finmind_price("2026-10-27", 50.0)]), ok([]), ok([]), ok([]), ok([])]
    stock = [
        ok([finmind_price("2026-10-27", 1000.0), finmind_price("2026-10-28", 995.0)]),
        ok([{"date": "2026-10-28", "before_price": 1000.0, "after_price": 995.0, "stock_or_cache_dividend": "息"}]),
        ok([{"CashEarningsDistribution": 4.5, "CashStatutorySurplus": 0.5, "CashExDividendTradingDate": "2026-10-28"}]),
        ok([]),
        ok([{"date": "2026-10-27", "end_date": "2026-10-30", "reason": "股東常會"}]),
    ]
    return [calendar, *([names] if info else []), split, ok([]), *etf, *stock, Answer(200, RATES)]


def test_a_tw_update_goes_through_every_phase(store):
    harness = Harness(store, *tw_answers(), start=TW_NOW)
    harness.run(lambda jobs: jobs.update("TW"))
    run = harness.last("UPDATE_TW")
    assert (run.outcome, run.message) == ("UPDATED", "bars 4, actions 2, suspensions 1, days 3, rates 2")
    assert store.trading_days("TW").tolist() == [to_day(date(2026, 10, d)) for d in (26, 27, 28)]
    assert [(s.symbol, s.name, s.exchange, s.category) for s in store.symbols("TW")] == [
        ("0050", "元大台灣50", "TWSE", "ETF"), ("2330", "台積電", "TWSE", "STOCK")]
    (split,) = store.actions("TW", "0050")
    (dividend,) = store.actions("TW", "2330")
    assert (split.kind, split.detail, split.ratio) == ("SHARE_RATIO", "SPLIT", 4.0)
    assert (dividend.kind, dividend.cash, dividend.ref_before, dividend.ref_after) == ("CASH_DIV", 5.0, 1000.0, 995.0)
    assert [s.reason for s in store.short_suspensions("2330")] == ["股東常會"]
    assert len(store.request_log("finmind")) == 14  # 4 for the market, 5 for each symbol
    # The same evening: no second INFO request; prices from 7 days back, actions from 400.
    again = Harness(store, *tw_answers(info=False), start=TW_NOW)
    again.run(lambda jobs: jobs.update("TW"))
    datasets = [call.params.get("dataset") for call in again.session.calls]
    assert "TaiwanStockInfo" not in datasets and again.last("UPDATE_TW").outcome == "UP_TO_DATE"
    prices = again.session.calls[datasets.index("TaiwanStockPrice")]
    results = again.session.calls[datasets.index("TaiwanStockDividendResult")]
    assert (prices.params["start_date"], results.params["start_date"]) == ("2026-10-20", "2025-09-22")


def test_finminds_maintenance_window_ends_the_update_as_maintenance(store):
    harness = Harness(store, start=datetime(2026, 11, 1, 3, 0, tzinfo=TAIPEI))
    harness.run(lambda jobs: jobs.update("TW"))
    assert harness.last("UPDATE_TW").outcome == "MAINTENANCE" and harness.session.calls == []
    assert harness.events()[-1] == ("UPDATE_FINISHED", None)


# ----- the schedule (§17.4) -----

SCHEDULE = ScheduleConfig(True, time(8, 30), time(18, 0))


def test_tick_starts_the_earliest_due_update_and_waits_while_data_is_busy(store):
    harness = Harness(store, keys=Keys("", "", ""))

    async def go():
        assert harness.jobs.tick(ScheduleConfig(False, time(8, 30), time(18, 0))) is None
        first = harness.jobs.tick(SCHEDULE)
        assert harness.jobs.tick(SCHEDULE) is None  # the DATA lane is busy
        await first.task
        second = harness.jobs.tick(SCHEDULE)
        await second.task
        return first, second, harness.jobs.tick(SCHEDULE)

    first, second, third = asyncio.run(go())
    assert (first.kind, first.scheduled_for) == ("UPDATE_TW", datetime(2026, 10, 27, 18, 0, tzinfo=TAIPEI))
    assert (second.kind, second.scheduled_for) == ("UPDATE_US", datetime(2026, 10, 28, 8, 30, tzinfo=TAIPEI))
    assert third is None  # both failed; their retries come 30 minutes later
    assert harness.last("UPDATE_TW").scheduled_for == first.scheduled_for


def test_update_failed_is_emailed_when_the_third_retry_fails(store):
    harness = Harness(store, keys=Keys("", "", ""))

    async def go():
        started = []
        for _ in range(5):
            while (job := harness.jobs.tick(SCHEDULE)) is not None:
                started.append(job.kind)
                await job.task
            harness.time.now += 31 * 60
        return started

    assert asyncio.run(go()) == ["UPDATE_TW", "UPDATE_US"] * 4
    assert harness.notes.sent == [
        ("UPDATE_FAILED", {"market": "TW", "error": "MISSING_KEY: the FinMind token is empty"}),
        ("UPDATE_FAILED", {"market": "US", "error": "MISSING_KEY: the Tiingo API key is empty"}),
    ]


# ----- backtest and optimization jobs (§17.5) -----

CONFIG = {"name": "job", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-06-03",
          "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 10, "slow": 30}}}
REQUEST = {"config": CONFIG, "min_trades": 0,
           "ranges": {"strategy.params.fast": {"values": [5, 10]}, "strategy.params.slow": {"values": [20, 40]}}}


@pytest.fixture
def market(tmp_path):
    opened = synthetic_store(tmp_path)
    yield opened
    opened.close()


def test_a_backtest_job_saves_its_result(market):
    harness = Harness(market)
    harness.run(lambda jobs: jobs.backtest(CONFIG))
    run = harness.last("BACKTEST")
    assert (run.lane, run.outcome, run.message) == ("COMPUTE", "DONE", "saved as backtest 1")
    validated = validate_config(CONFIG)
    direct = run_backtest(validated, load_run(market, None, validated), TAXES, strategy_factory(validated))
    saved = market.backtest(1)
    assert saved.metrics == direct.metrics and saved.config == direct.config
    assert_array_equal(market.backtest_series(1)["EQUITY"], direct.series["EQUITY"])
    assert harness.events() == [("BACKTEST_SAVED", None)]


def test_an_invalid_backtest_is_refused_before_any_job(market):
    with pytest.raises(RunConfigError):
        Harness(market).jobs.backtest({**CONFIG, "start": "soon"})
    assert market.latest_job_runs("BACKTEST", 1) == []


def test_an_optimization_job_saves_its_results(market):
    harness = Harness(market)
    job = harness.run(lambda jobs: jobs.optimization(REQUEST))
    row = market.optimization(1)
    assert (row.name, row.market, row.mode, row.status) == ("job", "US", "GRID", "DONE")
    assert len(row.results["combinations"]) == 4 and row.best["index"] in range(4)
    assert job.ready.result() is None and harness.last("OPTIMIZE").message == "saved as optimization 1"
    assert harness.events() == [("OPTIMIZATION_SAVED", None)]


def test_a_long_optimization_waits_for_confirmation(market):
    harness = Harness(market, step=1_000.0)  # every clock reading moves 1,000 s on: the estimate is 4,000 s
    job = harness.run(lambda jobs: jobs.optimization(REQUEST))
    assert job.ready.result() == NeedsConfirmation(4_000.0)
    assert harness.last("OPTIMIZE").outcome == "NEEDS_CONFIRMATION" and market.optimization(1) is None


def test_a_cancelled_optimization_keeps_its_partial_results(market):
    gate = threading.Semaphore(2)

    def gated(**options):
        pool = InlinePool(**options)
        submit = pool.submit

        def held(function, *args):
            return submit(lambda *call: gate.acquire() and function(*call), *args)

        pool.submit = held
        return pool

    harness = Harness(market, pool=gated)

    async def go():
        job = harness.jobs.optimization(REQUEST)
        while job.done < 2:
            await asyncio.sleep(0.01)
        harness.jobs.cancel("COMPUTE")
        gate.release()  # the third run, already started, finishes
        await job.task

    asyncio.run(go())
    row = market.optimization(1)
    assert row.status == "CANCELLED" and harness.last("OPTIMIZE").outcome == "CANCELLED"
    assert [entry["metrics"] is not None for entry in row.results["combinations"]] == [True, True, True, False]
    assert harness.events()[-1] == ("JOB_CANCELLED", None)
