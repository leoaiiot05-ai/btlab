"""IB minute bars: the link through a fake HistoryPort, normalization to raw prices and the MINUTE_BACKFILL job
(SPEC.md §5.4, §5.5, §15.1, §17.1, §17.3, AC-24).

The fake answers like ib_async's IB, the clock is injected, and nothing connects to any port.
"""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
import pytest
from numpy.testing import assert_allclose

from btlab.calendar import session
from btlab.config import IbConfig, ServiceConfig
from btlab.ib_link import IbError, IbLink, RequestError, account_kind
from btlab.jobs import Jobs, KeepAwake
from btlab.markets import MARKETS
from btlab.prices import normalize_minutes, share_ratio_after
from btlab.records import Action, Contract, MinuteBars, to_day
from btlab.store import MinuteStore, Store
from builders import TAXES, WEEKDAYS, put_bars
from fakes import Answer, FakeIB, FakeSession, FakeTime, InlinePool
from test_jobs import KEYS, NOW, RATES, SPY, AAA as AAA_ROWS

REPO = Path(__file__).resolve().parents[1]
IB = IbConfig("127.0.0.1", 4002, 31, "")
DAYS = [int(day) for day in WEEKDAYS[-6:]]
CONTRACT = Contract(72063691, "SMART", "USD", 1.0, 1.0, "26,26", NOW)
NO_DATA = "Historical Market Data Service error message:HMDS query returned no data: AAA@SMART Trades"
PACING = "Historical Market Data Service error message:Historical data request pacing violation"


def link(fake: FakeIB, time: FakeTime, mode: str = "test") -> IbLink:
    return IbLink(IB, mode, fake, clock=time.clock, sleep=time.sleep)


def bars(market: str, day: int, closes, offsets=None) -> list:
    """IB's answer: bars at minutes from the session's open, dated as ib_async gives them with formatDate=2."""
    start = session(market, day)[0]
    offsets = range(len(closes)) if offsets is None else offsets
    return [SimpleNamespace(date=datetime.fromtimestamp(start + 60 * m, timezone.utc), open=c - 0.1, high=c + 0.2,
                            low=c - 0.2, close=c, volume=100.0) for m, c in zip(offsets, closes)]


def details(con_id: int, exchange: str, currency: str, min_size: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(contract=SimpleNamespace(conId=con_id, exchange=exchange, currency=currency),
                           minSize=min_size, sizeIncrement=1.0, marketRuleIds="26,26")


# ----- the connection (§5.4, §15.1) -----


def test_the_link_connects_once_read_only_in_test_mode_with_the_configured_client_id():
    time = FakeTime()
    fake = FakeIB(time.clock)
    found = link(fake, time)
    asyncio.run(found.connect())
    asyncio.run(found.connect())
    assert fake.connects == [{"host": "127.0.0.1", "port": 4002, "clientId": 31, "timeout": 30.0, "readonly": True,
                              "account": ""}]
    assert (found.connected, found.account_kind) == (True, "paper")
    trade = FakeIB(time.clock, accounts=("U7654321", "DU1234567"))
    asyncio.run(IbLink(IbConfig("127.0.0.1", 4001, 31, "U7654321"), "trade", trade).connect())
    assert (trade.connects[0]["readonly"], trade.connects[0]["account"]) == (False, "U7654321")


def test_ibguards_client_id_is_refused():
    with pytest.raises(ValueError, match="17"):
        IbLink(IbConfig("127.0.0.1", 4002, 17, ""), "test", FakeIB(FakeTime().clock))


@pytest.mark.parametrize("refusal", [ConnectionRefusedError(), TimeoutError()])
def test_an_unreachable_gateway(refusal):
    fake = FakeIB(FakeTime().clock)
    fake.refusal = refusal
    with pytest.raises(IbError) as caught:
        asyncio.run(link(fake, FakeTime()).connect())
    assert caught.value.kind == "UNREACHABLE" and not fake.up


@pytest.mark.parametrize(("configured", "accounts", "kind"), [
    ("", ["DU1"], "paper"), ("U2", ["DU1", "U2"], "live"), ("", ["DU1", "U2"], None), ("", [], None)])
def test_the_account_kind(configured, accounts, kind):
    assert account_kind(configured, accounts) == kind


# ----- contracts (§5.4) -----


def test_contracts_resolve_to_ibs_only_match():
    time = FakeTime()
    fake = FakeIB(time.clock)
    fake.details = {"BRK B": [details(72063691, "SMART", "USD")], "2330": [details(6223250, "TWSE", "TWD", min_size=0.0)]}
    found = link(fake, time)
    us, tw = asyncio.run(found.contract("US", "BRK.B", None)), asyncio.run(found.contract("TW", "2330", "TWSE"))
    assert [(c.secType, c.symbol, c.exchange, c.currency) for c in fake.asked] == [
        ("STK", "BRK B", "SMART", "USD"), ("STK", "2330", "TWSE", "TWD")]
    assert us == Contract(72063691, "SMART", "USD", 1.0, 1.0, "26,26", NOW)
    assert (tw.con_id, tw.exchange, tw.currency, tw.min_size) == (6223250, "TWSE", "TWD", None)


@pytest.mark.parametrize(("market", "symbol", "exchange", "answer", "kind"), [
    ("US", "XYZ", None, [], "NO_PERMISSION"),
    ("US", "XYZ", None, RequestError(5, 200, "No security definition has been found for the request"), "NO_PERMISSION"),
    ("US", "XYZ", None, [details(1, "SMART", "USD"), details(2, "SMART", "USD")], "FAILED"),
    ("TW", "6488", None, [], "FAILED"),
])
def test_a_contract_ib_cannot_pin_down(market, symbol, exchange, answer, kind):
    fake = FakeIB(FakeTime().clock)
    fake.details = {symbol: answer}
    with pytest.raises(IbError) as caught:
        asyncio.run(link(fake, FakeTime()).contract(market, symbol, exchange))
    assert caught.value.kind == kind


# ----- the request (§5.4) -----


def test_one_request_per_symbol_and_session_as_section_5_4_says():
    time = FakeTime()
    day = DAYS[-1]
    start, end = session("US", day)
    fake = FakeIB(time.clock, lambda contract, end_time: bars("US", day, [10.0, 10.5, 11.0, 12.0], [0, 1, 389, 390]))
    found = asyncio.run(link(fake, time).minute_bars("US", "BRK.B", CONTRACT, day))
    (request,) = fake.requests
    assert {key: value for key, value in request.items() if key not in ("contract", "at")} == {
        "endDateTime": datetime.fromtimestamp(end, timezone.utc), "durationStr": "1 D", "barSizeSetting": "1 min",
        "whatToShow": "TRADES", "useRTH": True, "formatDate": 2, "timeout": 60.0}
    assert (request["contract"].symbol, request["contract"].exchange, request["contract"].conId) == ("BRK B", "SMART", 72063691)
    # The bar at the close starts after the regular session (§4.2).
    assert found.t.tolist() == [start, start + 60, start + 389 * 60] and found.close.tolist() == [10.0, 10.5, 11.0]


def test_requests_keep_two_seconds_apart_and_a_pacing_violation_waits_a_minute_once():
    time = FakeTime()
    answers = [bars("US", DAYS[0], [1.0]), RequestError(7, 162, PACING), bars("US", DAYS[1], [1.0]),
               RequestError(8, 162, PACING), RequestError(9, 162, PACING)]
    fake = FakeIB(time.clock, lambda contract, end: answers.pop(0))
    found = link(fake, time)

    async def go():
        await found.minute_bars("US", "AAA", CONTRACT, DAYS[0])
        await found.minute_bars("US", "AAA", CONTRACT, DAYS[1])
        with pytest.raises(IbError) as caught:
            await found.minute_bars("US", "AAA", CONTRACT, DAYS[2])
        return caught.value

    error = asyncio.run(go())
    assert np.diff([request["at"] for request in fake.requests]).tolist() == [2.0, 60.0, 2.0, 60.0]
    assert time.sleeps == [2.0, 60.0, 2.0, 60.0] and error.kind == "PACING"


@pytest.mark.parametrize(("answer", "kind"), [
    (RequestError(1, 162, NO_DATA), "NO_DATA"),
    (RequestError(1, 162, "Historical Market Data Service error message:No market data permissions for TWSE STK"),
     "NO_PERMISSION"),
    (RequestError(1, 354, "Requested market data is not subscribed."), "NO_PERMISSION"),
    (RequestError(1, 321, "Error validating request"), "FAILED"),
    (ConnectionError("Socket disconnect"), "UNREACHABLE"),
    ([], "FAILED"),
])
def test_what_ibs_answers_mean(answer, kind):
    time = FakeTime()
    with pytest.raises(IbError) as caught:
        asyncio.run(link(FakeIB(time.clock, lambda contract, end: answer), time).minute_bars("US", "AAA", CONTRACT, DAYS[0]))
    assert caught.value.kind == kind and "AAA" in str(caught.value)


# ----- normalization to raw prices (§5.4) -----


def minutes(closes) -> MinuteBars:
    close = np.array(closes, dtype=np.float64)
    return MinuteBars(np.arange(len(close), dtype=np.int64) * 60, close - 0.1, close + 0.2, close - 0.2, close,
                      np.full(len(close), 100.0))


@pytest.mark.parametrize(("daily", "status", "scale"), [(50.5, "OK", 1.0), (99.0, "OK", 2.0), (75.0, "MISMATCH", 1.0)])
def test_minute_bars_take_the_daily_closes_scale(daily, status, scale):
    """IB halves a day's bars before a 2:1 split; the stored daily close tells which scale a day has."""
    ib = minutes([49.0, 50.0])
    found, got, factor = normalize_minutes(ib, daily, 2.0)
    assert (got, factor) == (status, pytest.approx(daily / 50.0))
    assert_allclose(found.close, ib.close * scale)
    assert_allclose(found.low, ib.low * scale)
    assert_allclose(found.volume, ib.volume / scale)


def test_the_share_ratios_after_a_day():
    actions = [Action(10, "SHARE_RATIO", "SPLIT", ratio=2.0), Action(12, "CASH_DIV", "DIV_RESULT", cash=1.0),
               Action(15, "SHARE_RATIO", "SPLIT", ratio=3.0)]
    assert [share_ratio_after(actions, day) for day in (9, 10, 14, 15)] == [6.0, 3.0, 3.0, 1.0]


# ----- the MINUTE_BACKFILL job (§17.3) -----


@pytest.fixture
def stores(tmp_path):
    store, minute = Store(tmp_path), MinuteStore(tmp_path)
    days = np.array(DAYS, dtype=np.int64)
    put_bars(store, "US", "AAA", days, np.full(6, 100.0))
    put_bars(store, "TW", "2330", days, np.full(6, 500.0))
    store.set_symbol_details("TW", "2330", "TSMC", "TWSE")
    for market, symbol in (("US", "AAA"), ("TW", "2330")):
        store.set_minute_enabled(market, symbol, True)
    # A 2:1 split on the fifth day: IB halves the bars of the four days before it.
    store.replace_actions("US", "AAA", DAYS[0], DAYS[-1], [Action(DAYS[4], "SHARE_RATIO", "SPLIT", ratio=2.0)])
    yield store, minute
    store.close()
    minute.close()


def history(script: dict, default: float = 100.0):
    """IB's answers by (symbol, day): an exception to raise, or the last close of the day's bars."""
    def answer(contract, end: datetime):
        market = "TW" if contract.currency == "TWD" else "US"
        day = to_day(end.astimezone(ZoneInfo(MARKETS[market].time_zone)).date())
        found = script.get((contract.symbol, day), default)
        return found if isinstance(found, BaseException) else bars(market, day, [found - 1.0, found])

    return answer


def answered(fake: FakeIB) -> list[tuple[str, int]]:
    tz = {"USD": ZoneInfo(MARKETS["US"].time_zone), "TWD": ZoneInfo(MARKETS["TW"].time_zone)}
    return [(r["contract"].symbol, to_day(r["endDateTime"].astimezone(tz[r["contract"].currency]).date())) for r in fake.requests]


def make_jobs(store: Store, minute: MinuteStore, fake: FakeIB, time: FakeTime, **options) -> Jobs:
    return Jobs(store, minute, KEYS, TAXES, lambda: {}, InlinePool, 1, clock=time.clock, sleep=time.sleep,
                keep_awake=KeepAwake(lambda flags: None), ib=link(fake, time), **options)


def backfill(store: Store, minute: MinuteStore, fake: FakeIB, time: FakeTime):
    async def go():
        job = make_jobs(store, minute, fake, time).backfill()
        await job.task

    asyncio.run(go())
    return store.latest_job_runs("MINUTE_BACKFILL", 1)[0]


def statuses(minute: MinuteStore, market: str, symbol: str) -> dict[int, tuple[str, float | None]]:
    return {c.day: (c.status, c.factor and round(c.factor, 4)) for c in minute.minute_coverage(market, symbol)}


def test_a_backfill_stores_each_day_newest_first_with_its_coverage(stores):
    store, minute = stores
    time = FakeTime()
    fake = FakeIB(time.clock, history({
        ("AAA", DAYS[3]): 50.0, ("AAA", DAYS[2]): RequestError(3, 162, NO_DATA),
        ("AAA", DAYS[1]): RequestError(4, 321, "Error validating request"), ("AAA", DAYS[0]): 70.0,
        ("2330", DAYS[5]): RequestError(5, 354, "Requested market data is not subscribed.")}))
    fake.details = {"AAA": [details(1, "SMART", "USD")], "2330": [details(2, "TWSE", "TWD")]}
    run = backfill(store, minute, fake, time)
    # TW's first answer stops TW; US goes on, newest first.
    assert answered(fake) == [("2330", DAYS[5]), *(("AAA", day) for day in reversed(DAYS))]
    assert min(np.diff([request["at"] for request in fake.requests])) >= 2.0
    assert statuses(minute, "US", "AAA") == {DAYS[5]: ("OK", 1.0), DAYS[4]: ("OK", 1.0), DAYS[3]: ("OK", 2.0),
                                             DAYS[2]: ("EMPTY", None), DAYS[1]: ("FAILED", None),
                                             DAYS[0]: ("MISMATCH", 1.4286)}
    assert statuses(minute, "TW", "2330") == {DAYS[5]: ("NO_PERMISSION", None)}
    split = minute.minute_bars("US", "AAA", *session("US", DAYS[3]))
    assert split.close.tolist() == [98.0, 100.0] and split.volume.tolist() == [50.0, 50.0]
    assert (run.lane, run.outcome, run.message) == ("MINUTE", "UPDATED", "EMPTY 1, FAILED 1, MISMATCH 1, NO_PERMISSION 1, OK 3")
    events = [(e.kind, e.market, e.symbol) for e in reversed(store.latest_events(10))]
    assert events == [("MINUTE_NO_PERMISSION", "TW", "2330"), ("MINUTE_MISMATCH", "US", "AAA"), ("UPDATE_FINISHED", None, None)]
    assert (store.setting("ib_permission/US"), store.setting("ib_permission/TW")) == ("OK", "NO_PERMISSION")
    assert [c.symbol for c in fake.asked] == ["2330", "AAA"] and store.contract("US", "AAA").con_id == 1
    # The next run retries the FAILED day only; TW waits for its next update.
    again = FakeIB(time.clock, history({}))
    backfill(store, minute, again, time)
    assert answered(again) == [("AAA", DAYS[1])] and again.asked == []


def test_a_cancelled_backfill_resumes_where_it_stopped(stores):
    store, minute = stores
    store.set_minute_enabled("TW", "2330", False)
    time = FakeTime()
    fake = FakeIB(time.clock)
    fake.details = {"AAA": [details(1, "SMART", "USD")]}

    async def go():
        jobs = make_jobs(store, minute, fake, time)
        answer = history({}, default=100.0)

        def cancelling(contract, end):
            if len(fake.requests) == 3:
                jobs.cancel("MINUTE")
            return answer(contract, end)

        fake.history = cancelling
        job = jobs.backfill()
        await job.task

    asyncio.run(go())
    assert store.latest_job_runs("MINUTE_BACKFILL", 1)[0].outcome == "CANCELLED"
    assert sorted(statuses(minute, "US", "AAA")) == DAYS[4:]
    rest = FakeIB(time.clock, history({}, default=50.0))
    backfill(store, minute, rest, time)
    assert answered(rest) == [("AAA", day) for day in reversed(DAYS[:4])]
    assert {status for status, _ in statuses(minute, "US", "AAA").values()} == {"OK"}


def test_ib_unreachable_ends_the_backfill(stores):
    store, minute = stores
    time = FakeTime()
    refused = FakeIB(time.clock)
    refused.refusal = ConnectionRefusedError()
    run = backfill(store, minute, refused, time)
    assert (run.outcome, run.message) == ("FAILED", "IB_UNREACHABLE: IB cannot be reached (ConnectionRefusedError)")
    dropped = FakeIB(time.clock, history({("2330", DAYS[4]): ConnectionError("Socket disconnect")}))
    dropped.details = {"AAA": [details(1, "SMART", "USD")], "2330": [details(2, "TWSE", "TWD")]}
    run = backfill(store, minute, dropped, time)
    assert run.outcome == "FAILED" and run.message.startswith("IB_UNREACHABLE: the IB connection is lost")
    # What came before the drop stays.
    assert set(statuses(minute, "TW", "2330")) == {DAYS[5]} and set(statuses(minute, "US", "AAA")) == {DAYS[5]}


class HeldSession:
    """A DATA job's session: each request waits until released, then fails as if spylead were down."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __aenter__(self) -> "HeldSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    def request(self, method: str, url: str, **options) -> "_Held":
        return _Held(self.release)


class _Held:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release

    async def __aenter__(self):
        await self.release.wait()
        raise aiohttp.ClientConnectionError()

    async def __aexit__(self, *exc_info) -> None:
        return None


def test_the_minute_lane_waits_while_a_data_job_runs(stores):
    store, minute = stores
    store.set_minute_enabled("TW", "2330", False)
    time = FakeTime()
    fake = FakeIB(time.clock, history({}))
    fake.details = {"AAA": [details(1, "SMART", "USD")]}
    held = HeldSession()

    async def go():
        jobs = make_jobs(store, minute, fake, time, session=lambda: held,
                         spylead=ServiceConfig("http://127.0.0.1:8090", "Sentinel-Spylead-Token-2e5d"))
        data = jobs.snapshot()
        job = jobs.backfill()
        for _ in range(20):
            await asyncio.sleep(0)
        paused = (job.phase, job.message, len(fake.requests))
        held.release.set()
        await data.task
        await job.task
        return paused

    assert asyncio.run(go()) == ("MINUTES", "paused while a data job runs", 0)
    assert len(fake.requests) == 6 and store.latest_job_runs("MINUTE_BACKFILL", 1)[0].outcome == "UPDATED"


def test_an_update_lifts_its_markets_stop_and_starts_a_backfill(stores):
    store, minute = stores
    store.set_minute_enabled("TW", "2330", False)
    store.save_setting("ib_permission/US", "NO_PERMISSION")
    store.set_list_symbols(store.create_list("mine", "US", NOW), ["AAA"])
    time = FakeTime()
    # The update stores AAA's closes of 50 and 51 (test_jobs.AAA); IB's bars match them.
    updated = {to_day(datetime(2026, 10, 26).date()): 50.0, to_day(datetime(2026, 10, 27).date()): 51.0}
    fake = FakeIB(time.clock, history({("AAA", day): close for day, close in updated.items()}))
    fake.details = {"AAA": [details(1, "SMART", "USD")]}

    async def go():
        session = FakeSession(Answer(200, SPY), Answer(200, AAA_ROWS), Answer(200, RATES))
        jobs = make_jobs(store, minute, fake, time, session=lambda: session)
        await jobs.update("US").task
        await jobs.lane_job("MINUTE").task

    asyncio.run(go())
    # The new sessions come first; the old ones are more than 3 years before the latest bar.
    assert answered(fake) == [("AAA", day) for day in sorted(updated, reverse=True)]
    assert store.setting("ib_permission/US") == "OK"
    assert [run.outcome for run in store.latest_job_runs("MINUTE_BACKFILL", 5)] == ["UPDATED"]


# ----- the boundary (§5.4, §15.1) -----


def test_only_ib_link_and_ib_orders_import_ib_async():
    """No other module, and no test: tests reach IB through fakes."""
    importing = re.compile(r"^\s*(?:import|from)\s+ib_async\b", re.M)
    found = {path.relative_to(REPO).as_posix()
             for folder in REPO.iterdir() if folder.is_dir() and not folder.name.startswith(".")
             for path in folder.rglob("*.py") if importing.search(path.read_text(encoding="utf-8"))}
    assert "btlab/ib_link.py" in found and found <= {"btlab/ib_link.py", "btlab/ib_orders.py"}
