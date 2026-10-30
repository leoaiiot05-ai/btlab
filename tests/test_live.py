"""Live portfolios in test mode: the batch from the replay, the virtual book, the ledger and the routes (SPEC.md §14,
§17.2, §17.4, §19, AC-25).

Stores are temporary, data synthetic, the clock injected and the pool in process; nothing connects to IB.
"""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from btlab.backtest import load_run, run_backtest, save_result, strategy_factory, validate_config
from btlab.config import ScheduleConfig, load_config
from btlab.costs import CostModel
from btlab.jobs import CURVES_KEY, Jobs, KeepAwake, LiveError
from btlab.live import (Bar, Book, batch_lines, corporate_actions, cutoff, fill_price, live_lot, replay_targets, settle,
                        target_quantity, target_weights)
from btlab.markets import MARKETS
from btlab.records import DailyBars, Line, to_day
from btlab.store import MinuteStore, Store
from btlab.web import Services, make_app
from builders import TAXES, WEEKDAYS, synthetic_store
from fakes import Answer, FakeSession, InlinePool
from test_config import VALID, render
from test_jobs import AAA, KEYS, RATES, SPY, Notes

TOKEN = VALID["web"]["token"]
LAST = int(WEEKDAYS[-1])  # Thursday 31 December 2020
SESSION = to_day(date(2021, 1, 1))  # the Friday after: the next session of a US batch
BEFORE_CUTOFF = datetime(2020, 12, 31, 23, 0, tzinfo=timezone.utc)
HOLD = {"name": "hold", "market": "US", "symbols": ["AAA", "BBB"], "start": "2020-06-01",
        "strategy": {"source": "builtin", "id": "BUY_HOLD", "params": {}}}
US = CostModel("US", MARKETS["US"].commission, 5.0, 30.0, ("STOCK",))
TW = CostModel("TW", MARKETS["TW"].commission, 10.0, 0.0, ("STOCK",), TAXES, short_fee_pct=0.08)


def line(action="BUY", quantity=10.0, kind="MOO", limit=None, symbol="AAA") -> Line:
    return Line(symbol, action, quantity, kind, limit, 100.0, "VIRTUAL_PENDING")


# ----- a batch (§14.2) -----


def test_replay_targets_put_each_pending_target_in_place_of_the_position():
    assert replay_targets({"AAA": 10.0, "BBB": 5.0, "CCC": 0.0}, {"BBB": 0.0, "CCC": -3.0}) == {"AAA": 10.0, "BBB": 0.0,
                                                                                                "CCC": -3.0}


def test_weights_scale_to_the_live_equity_in_live_lots():
    weights = target_weights({"AAA": 10.0, "BBB": 0.0, "CCC": -4.0}, {"AAA": 50.0, "BBB": 9.0, "CCC": 25.0}, 2_000.0)
    assert weights == {"AAA": 0.25, "BBB": 0.0, "CCC": -0.05}
    assert (live_lot("US", None), live_lot("TW", None), live_lot("TW", 1.0)) == (1.0, 1000.0, 1.0)
    # Toward zero to the lot, on both sides.
    assert target_quantity(0.25, 10_000.0, 33.0, 1.0) == 75.0
    assert target_quantity(0.5, 1_000_000.0, 70.0, 1000.0) == 7000.0
    assert target_quantity(-0.05, 10_000.0, 33.0, 1.0) == -15.0


def test_lines_with_their_order_kinds_limits_and_skips():
    weights = {"AAA": 0.5, "BBB": 0.0, "CCC": 0.2, "DDD": 0.001}
    closes = {"AAA": 100.0, "BBB": 50.0, "CCC": None, "DDD": 400.0}
    found = batch_lines("US", weights, closes, {"BBB": 20.0}, 10_000.0, dict.fromkeys(weights, 1.0), SESSION, "LIMIT", 1.0)
    assert found == [
        Line("AAA", "BUY", 50.0, "LIMIT", pytest.approx(101.0), 100.0, "PROPOSED"),
        Line("BBB", "SELL", 20.0, "LIMIT", pytest.approx(49.5), 50.0, "PROPOSED"),
        Line("CCC", "BUY", 0.0, "LIMIT", None, 0.0, "SKIPPED_NO_PRICE", "no close is stored"),
    ]  # DDD's 0.025 shares are less than one lot.
    assert [x.kind for x in batch_lines("US", {"AAA": 0.5}, {"AAA": 100.0}, {}, 10_000.0, {"AAA": 1.0}, SESSION, "MOO", 1.0)] == ["MOO"]


def test_tw_lines_trade_board_lots_never_open_a_short_and_stay_inside_the_price_limits():
    lots = {"2330": 1000.0, "0050": 1000.0, "2454": 1000.0}
    found = batch_lines("TW", {"2330": 0.3, "0050": -0.1, "2454": 0.004}, {"2330": 500.0, "0050": 100.0, "2454": 900.0},
                        {}, 2_000_000.0, lots, SESSION, "MOO", 15.0)
    assert [(x.symbol, x.action, x.quantity, x.kind, x.status) for x in found] == [
        ("2330", "BUY", 1000.0, "LIMIT", "PROPOSED"), ("0050", "SELL", 2000.0, "LIMIT", "SKIPPED_TW_SHORT")]
    # A 15% offset is clipped to the 10% daily limit.
    assert found[0].limit_price == pytest.approx(550.0)


def test_the_cutoffs():
    assert cutoff("US", SESSION).astimezone(timezone.utc) == datetime(2021, 1, 1, 14, 20, tzinfo=timezone.utc)
    assert cutoff("US", to_day(date(2021, 7, 1))).astimezone(timezone.utc) == datetime(2021, 7, 1, 13, 20, tzinfo=timezone.utc)
    assert cutoff("TW", SESSION).astimezone(timezone.utc) == datetime(2021, 1, 1, 0, 50, tzinfo=timezone.utc)


# ----- the virtual book (§14.5) -----


@pytest.mark.parametrize(("order", "bar", "price"), [
    (line(), Bar(100.0, 104.0, 98.0, 103.0, True, 0), 100.0),
    (line(kind="LIMIT", limit=101.0), Bar(100.0, 104.0, 98.0, 103.0, True, 0), 100.0),
    (line(kind="LIMIT", limit=99.0), Bar(100.0, 104.0, 98.0, 103.0, True, 0), 99.0),
    (line(kind="LIMIT", limit=97.0), Bar(100.0, 104.0, 98.0, 103.0, True, 0), None),
    (line("SELL", kind="LIMIT", limit=99.0), Bar(100.0, 104.0, 98.0, 103.0, True, 0), 100.0),
    (line("SELL", kind="LIMIT", limit=103.5), Bar(100.0, 104.0, 98.0, 103.0, True, 0), 103.5),
    (line("SELL", kind="LIMIT", limit=105.0), Bar(100.0, 104.0, 98.0, 103.0, True, 0), None),
    (line(), Bar(100.0, 104.0, 98.0, 103.0, False, 0), None),
    (line(), Bar(110.0, 110.0, 110.0, 110.0, True, 1), None),
    (line("SELL"), Bar(110.0, 110.0, 110.0, 110.0, True, 1), 110.0),
    (line("SELL"), Bar(90.0, 90.0, 90.0, 90.0, True, -1), None),
], ids=["moo", "limit buy at the open", "limit buy at the limit", "limit buy expires", "limit sell at the open",
        "limit sell at the limit", "limit sell expires", "untradable", "locked up buy", "locked up sell", "locked down sell"])
def test_a_confirmed_line_fills_by_the_backtest_rules(order, bar, price):
    assert fill_price(order, bar) == price


def test_fills_pay_slippage_and_costs_but_never_past_the_limit():
    book = Book(10_000.0, {"AAA": 5.0})
    after, paid, charge = settle(book, line(quantity=10.0), 100.0, US, 0, SESSION)
    assert paid == pytest.approx(100.05) and charge == pytest.approx(1.0)  # 10 × 0.005 is below the 1 USD minimum
    assert after == Book(pytest.approx(10_000.0 - 1000.5 - 1.0), {"AAA": 15.0})
    _, paid, _ = settle(book, line(kind="LIMIT", limit=100.02), 100.0, US, 0, SESSION)
    assert paid == 100.02
    sold, paid, charge = settle(Book(0.0, {"2330": 2000.0}), line("SELL", 1000.0, symbol="2330"), 500.0, TW, 0, SESSION)
    value = 1000.0 * 500.0 * (1 - 0.001)
    # TW: 0.08% commission and the 0.3% transaction tax on the sale.
    assert paid == pytest.approx(499.5) and charge == pytest.approx(value * 0.0008 + value * 0.003)
    assert sold.holdings == {"2330": 1000.0} and sold.cash == pytest.approx(value - charge)


def test_a_split_and_a_dividend_on_the_virtual_book():
    book = Book(1_000.0, {"AAA": 5.0, "BBB": -5.0, "CCC": 10.0})
    after = corporate_actions(book, {"AAA": 1.5, "BBB": 1.5}, {"CCC": 2.0}, {"AAA": 30.0, "BBB": 30.0, "CCC": 50.0}, 30.0)
    # 7.5 shares: 7, and half a new share (30 ÷ 1.5 = 20) in cash; the short is debited the same.
    assert after.holdings == {"AAA": 7.0, "BBB": -7.0, "CCC": 10.0}
    assert after.cash == pytest.approx(1_000.0 + 10.0 - 10.0 + 10.0 * 2.0 * 0.7)
    shorted = corporate_actions(Book(0.0, {"CCC": -10.0}), {}, {"CCC": 2.0}, {"CCC": 50.0}, 30.0)
    assert shorted.cash == -20.0


# ----- the jobs (§14.2 to §14.6, §17.2, §17.4) -----


class Clock:
    def __init__(self, start: datetime = BEFORE_CUTOFF) -> None:
        self.now = start.timestamp()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def market(tmp_path):
    store = synthetic_store(tmp_path)
    config = validate_config(HOLD)
    backtest_id = save_result(store, run_backtest(config, load_run(store, None, config), TAXES, strategy_factory(config)),
                              BEFORE_CUTOFF)
    yield store, backtest_id
    store.close()


def jobs_of(store: Store, clock: Clock, notes: Notes | None = None, **options) -> Jobs:
    return Jobs(store, None, KEYS, TAXES, lambda: {}, InlinePool, 1, clock=clock.clock, sleep=clock.sleep,
                keep_awake=KeepAwake(lambda flags: None), notifier=notes, **options)


def run(jobs: Jobs, start) -> None:
    async def go():
        await start(jobs).task
    asyncio.run(go())


def session_bars(store: Store, day: int, prices: dict[str, tuple[float, float, float, float]]) -> None:
    for symbol, (open_, high, low, close) in prices.items():
        store.upsert_daily_bars("US", symbol, DailyBars(np.array([day]), np.array([open_]), np.array([high]),
                                                        np.array([low]), np.array([close]), np.array([1e6]),
                                                        np.array([False])))


def test_creating_portfolios_daily_only_and_one_active_per_market_and_mode(market, tmp_path):
    store, backtest_id = market
    jobs = jobs_of(store, Clock())
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), None)
    found = store.portfolio(portfolio_id)
    assert (found.market, found.mode, found.capital, found.first_day, found.config["name"]) == (
        "US", "test", MARKETS["US"].initial_capital, SESSION, "hold")
    with pytest.raises(LiveError) as again:
        jobs.create_portfolio(store.backtest(backtest_id), 5_000.0)
    assert again.value.code == "ALREADY_EXISTS"
    # The other run mode has its own.
    trade = jobs_of(store, Clock(), mode="trade").create_portfolio(store.backtest(backtest_id), 5_000.0)
    assert store.portfolio(trade).capital == 5_000.0
    with pytest.raises(LiveError) as minute:
        jobs.create_portfolio(replace(store.backtest(backtest_id), timeframe="15m"), None)
    assert minute.value.code == "NOT_DAILY"


def test_a_batch_follows_the_replay_and_replaces_the_proposed_one(market):
    store, backtest_id = market
    notes = Notes()
    jobs = jobs_of(store, Clock(), notes)
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), 100_000.0)
    run(jobs, lambda j: j.generate("US"))
    (batch,) = store.batches(portfolio_id, 5)
    replay = run_backtest(validate_config(HOLD), load_run(store, None, validate_config(HOLD)), TAXES,
                          strategy_factory(validate_config(HOLD)))
    closes = {s: float(store.daily_bars("US", s).close[-1]) for s in HOLD["symbols"]}
    weights = {s: replay.holdings[s] * closes[s] / replay.curve.equity[-1] for s in HOLD["symbols"]}
    assert (batch.status, batch.session_day, batch.positions_seen) == ("PROPOSED", SESSION, {})
    assert [(x.symbol, x.action, x.quantity, x.kind, x.status) for x in batch.lines] == [
        (s, "BUY", float(int(weights[s] * 100_000.0 / closes[s])), "MOO", "PROPOSED") for s in HOLD["symbols"]]
    assert notes.sent == [("BATCH_READY", {"market": "US", "session": "2021-01-01", "cutoff": "2021-01-01 09:20 EST",
                                           "lines": "2"})]
    curves = store.setting(CURVES_KEY.format(portfolio=portfolio_id))
    assert curves["t"] == [LAST] and len(curves["replay"]) == len(curves["benchmark"]) == 1
    run(jobs, lambda j: j.generate("US"))
    assert [b.status for b in store.batches(portfolio_id, 5)] == ["PROPOSED", "REPLACED"]
    assert store.latest_events(1)[0].kind == "BATCH_PROPOSED"


def test_confirming_expiry_and_the_lock(market):
    store, backtest_id = market
    clock = Clock()
    jobs = jobs_of(store, clock)
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), None)
    run(jobs, lambda j: j.generate("US"))
    batch = store.batches(portfolio_id, 1)[0]
    jobs.confirm(batch, store.portfolio(portfolio_id), {batch.lines[0].id})
    confirmed = store.batch(batch.id)
    assert (confirmed.status, [x.status for x in confirmed.lines]) == ("CONFIRMED", ["VIRTUAL_PENDING", "SKIPPED_OWNER"])
    with pytest.raises(LiveError) as twice:
        jobs.confirm(confirmed, store.portfolio(portfolio_id), set())
    assert twice.value.code == "BATCH_EXPIRED"
    with pytest.raises(LiveError) as locked:
        jobs.regenerate(store.portfolio(portfolio_id))
    assert locked.value.code == "BATCH_LOCKED"
    # A later batch left unconfirmed expires at the cutoff, through the scheduler's tick.
    store.set_batch_status(batch.id, "DONE")
    run(jobs, lambda j: j.generate("US"))
    late = store.batches(portfolio_id, 1)[0]
    clock.now = (cutoff("US", SESSION) - timedelta(seconds=1)).timestamp()
    jobs.expire()
    assert store.batch(late.id).status == "PROPOSED"
    clock.now += 1
    jobs.tick(ScheduleConfig(False, (), ()))
    assert store.batch(late.id).status == "EXPIRED" and store.latest_events(1)[0].kind == "BATCH_EXPIRED"
    with pytest.raises(LiveError):
        jobs.confirm(store.batch(late.id), store.portfolio(portfolio_id), {late.lines[0].id})


def test_virtual_fills_the_ledger_and_the_curve(market):
    store, backtest_id = market
    jobs = jobs_of(store, Clock())
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), 100_000.0)
    run(jobs, lambda j: j.generate("US"))
    batch = store.batches(portfolio_id, 1)[0]
    jobs.confirm(batch, store.portfolio(portfolio_id), {x.id for x in batch.lines})
    prices = {"SPY": (300.0, 302.0, 299.0, 301.0), "AAA": (50.0, 51.0, 49.0, 50.5), "BBB": (20.0, 20.5, 19.5, 20.2)}
    session_bars(store, SESSION, prices)
    run(jobs, lambda j: j.virtual_fill("US"))
    done = store.batch(batch.id)
    assert done.status == "DONE" and [x.status for x in done.lines] == ["FILLED", "FILLED"]
    quantities = {x.symbol: x.quantity for x in batch.lines}
    assert store.holdings(portfolio_id) == quantities
    spent = sum(x.filled_qty * x.avg_price + x.commission for x in done.lines)
    assert [x.avg_price for x in done.lines] == [pytest.approx(prices[s][0] * 1.0005) for s in quantities]
    (row,) = store.ledger(portfolio_id)
    assert row.day == SESSION and row.cash == pytest.approx(100_000.0 - spent)
    assert row.equity == pytest.approx(row.cash + sum(q * prices[s][3] for s, q in quantities.items()))
    # Nothing new to fill: the next run writes no row.
    run(jobs, lambda j: j.virtual_fill("US"))
    assert len(store.ledger(portfolio_id)) == 1


def test_a_session_without_a_bar_expires_its_lines_and_a_split_reaches_the_book(market):
    store, backtest_id = market
    jobs = jobs_of(store, Clock())
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), 100_000.0)
    run(jobs, lambda j: j.generate("US"))
    batch = store.batches(portfolio_id, 1)[0]
    jobs.confirm(batch, store.portfolio(portfolio_id), {x.id for x in batch.lines})
    monday = to_day(date(2021, 1, 4))
    session_bars(store, monday, {"SPY": (300.0, 302.0, 299.0, 301.0), "AAA": (50.0, 51.0, 49.0, 50.5),
                                 "BBB": (20.0, 20.5, 19.5, 20.2)})
    run(jobs, lambda j: j.virtual_fill("US"))
    assert [x.status for x in store.batch(batch.id).lines] == ["EXPIRED", "EXPIRED"]
    assert [row.day for row in store.ledger(portfolio_id)] == [monday] and store.holdings(portfolio_id) == {}


def test_data_incomplete_on_the_latest_session_makes_no_batch(market):
    store, backtest_id = market
    jobs = jobs_of(store, Clock())
    portfolio_id = jobs.create_portfolio(store.backtest(backtest_id), None)
    session_bars(store, SESSION, {"SPY": (300.0, 302.0, 299.0, 301.0), "AAA": (50.0, 51.0, 49.0, 50.5)})
    run(jobs, lambda j: j.generate("US"))
    assert store.batches(portfolio_id, 5) == []
    run_row = store.latest_job_runs("LIVE_GENERATE", 1)[0]
    assert run_row.outcome == "FAILED" and run_row.message.startswith("LIVE_DATA_INCOMPLETE: no bar on 2021-01-01 for BBB")
    assert [e.kind for e in store.latest_events(2)] == ["JOB_FAILED", "LIVE_DATA_INCOMPLETE"]


def test_an_update_fills_then_generates(market):
    store, backtest_id = market
    store.set_list_symbols(store.create_list("mine", "US", BEFORE_CUTOFF), ["AAA"])
    session = FakeSession(Answer(200, SPY), Answer(200, AAA), Answer(200, RATES))
    jobs = jobs_of(store, Clock(datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc)), session=lambda: session)
    jobs.create_portfolio(store.backtest(backtest_id), None)

    async def go():
        await jobs.update("US").task
        while (live := jobs.lane_job("LIVE")) is not None:
            await live.task

    asyncio.run(go())
    assert store.latest_job_runs("LIVE_VIRTUAL_FILL", 1)[0].outcome == "UPDATED"
    # BBB has no bar on the update's latest session.
    assert store.latest_job_runs("LIVE_GENERATE", 1)[0].message.startswith("LIVE_DATA_INCOMPLETE")


# ----- the routes (§19) -----


def serve(tmp_path, store: Store, scenario, clock: Clock | None = None):
    (tmp_path / "config.toml").write_text(render(VALID), encoding="utf-8")
    (tmp_path / "strategies").mkdir(exist_ok=True)
    clock = clock or Clock()
    minute = MinuteStore(tmp_path)

    async def go():
        jobs = jobs_of(store, clock)
        services = Services(load_config(tmp_path / "config.toml"), store, minute, jobs, None, clock.clock)
        async with TestClient(TestServer(make_app(services))) as client:
            assert (await client.post("/api/login", json={"token": TOKEN})).status == 204
            return await scenario(client, jobs)

    try:
        return asyncio.run(go())
    finally:
        minute.close()


async def answer(response) -> tuple[int, object]:
    return response.status, (await response.json() if response.content_type == "application/json" else None)


def test_the_live_routes_in_test_mode(market, tmp_path):
    store, backtest_id = market

    async def scenario(client, jobs):
        found = {"create": await answer(await client.post("/api/live", json={"backtest_id": backtest_id, "capital": 50_000})),
                 "again": await answer(await client.post("/api/live", json={"backtest_id": backtest_id})),
                 "missing": await answer(await client.post("/api/live", json={"backtest_id": 99})),
                 "bad": await answer(await client.post("/api/live", json={"backtest_id": backtest_id, "capital": -1}))}
        portfolio_id = found["create"][1]["id"]
        found["regenerate"] = await answer(await client.post(f"/api/live/{portfolio_id}/regenerate", json={}))
        await jobs.lane_job("LIVE").task
        batch = store.batches(portfolio_id, 1)[0]
        found["state"] = await answer(await client.get("/api/state"))
        found["confirm"] = await answer(await client.post(f"/api/live/batches/{batch.id}/confirm",
                                                          json={"lines": [x.id for x in batch.lines]}))
        found["expired"] = await answer(await client.post(f"/api/live/batches/{batch.id}/confirm", json={"lines": []}))
        found["locked"] = await answer(await client.post(f"/api/live/{portfolio_id}/regenerate", json={}))
        session_bars(store, SESSION, {"SPY": (300.0, 302.0, 299.0, 301.0), "AAA": (50.0, 51.0, 49.0, 50.5),
                                      "BBB": (20.0, 20.5, 19.5, 20.2)})
        await jobs.virtual_fill("US").task
        found["list"] = await answer(await client.get("/api/live"))
        found["batches"] = await answer(await client.get(f"/api/live/{portfolio_id}/batches"))
        found["curve"] = await answer(await client.get(f"/api/live/{portfolio_id}/curve"))
        found["delete"] = await answer(await client.delete(f"/api/live/{portfolio_id}"))
        found["deleted"] = await answer(await client.delete(f"/api/live/{portfolio_id}"))
        return found, batch

    found, batch = serve(tmp_path, store, scenario)
    assert found["create"][0] == 201 and found["again"][1]["error"] == "ALREADY_EXISTS"
    assert found["missing"][0] == 404 and (found["bad"][0], found["bad"][1]["path"]) == (400, "capital")
    assert found["regenerate"] == (202, {"lane": "LIVE", "kind": "LIVE_GENERATE"})
    assert found["state"][1]["live"]["US"] == {"portfolio_id": 1, "batch": {
        "id": batch.id, "status": "PROPOSED", "session": "2021-01-01", "cutoff": "2021-01-01T14:20:00Z", "lines": 2}}
    assert found["confirm"] == (202, {"batch": batch.id, "status": "CONFIRMED", "job": None})
    assert found["expired"][1]["error"] == "BATCH_EXPIRED" and found["locked"][1]["error"] == "BATCH_LOCKED"
    (portfolio,) = found["list"][1]
    assert (portfolio["name"], portfolio["capital"], portfolio["first_day"]) == ("hold", 50_000.0, "2021-01-01")
    assert [h["symbol"] for h in portfolio["holdings"]] == ["AAA", "BBB"] and portfolio["batch"]["status"] == "DONE"
    assert sum(h["weight"] for h in portfolio["holdings"]) == pytest.approx(1 - portfolio["cash"] / portfolio["equity"], abs=1e-5)
    assert [x["status"] for x in found["batches"][1][0]["lines"]] == ["FILLED", "FILLED"]
    curve = found["curve"][1]
    assert curve["t"] == [LAST, SESSION] and curve["series"]["PORTFOLIO"] == [None, portfolio["equity"]]
    assert curve["series"]["REPLAY"][0] == curve["series"]["BENCHMARK"][0] == 50_000.0
    assert found["delete"][0] == 204 and found["deleted"][0] == 404
