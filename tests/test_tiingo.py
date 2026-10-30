"""Tiingo: requests, translation and the budget (SPEC.md §5.1, AC-3)."""

import asyncio
import logging
from datetime import date

import aiohttp
import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab import tiingo
from btlab.http import RequestBudget, Requester, SourceError
from btlab.records import Action, to_day
from btlab.tiingo import TiingoClient, tiingo_ticker, translate_prices
from fakes import Answer, FakeSession, FakeTime

KEY = "Sentinel-Tiingo-Key-5e2d"
FIRST, LAST = to_day(date(2026, 10, 21)), to_day(date(2026, 10, 28))


def row(iso: str, close: float, div: float = 0.0, split: float = 1.0, volume: float = 1_000_000) -> dict:
    return {
        "date": f"{iso}T00:00:00.000Z", "open": close - 1, "high": close + 2, "low": close - 2, "close": close,
        "volume": volume, "adjOpen": 0.5, "adjHigh": 0.5, "adjLow": 0.5, "adjClose": 0.5, "adjVolume": 2,
        "divCash": div, "splitFactor": split,
    }


def client(session, key=KEY, time=None, budget=None) -> TiingoClient:
    time = time or FakeTime()
    return TiingoClient(Requester("Tiingo", session, sleep=time.sleep, clock=time.clock, budget=budget), key)


def daily(tiingo_client: TiingoClient, symbol: str = "SPY"):
    return asyncio.run(tiingo_client.daily(symbol, FIRST, LAST))


def test_tickers_with_a_class_take_a_hyphen():
    assert [tiingo_ticker(symbol) for symbol in ("BRK.B", "SPY", "BF.A")] == ["BRK-B", "SPY", "BF-A"]


def test_a_price_request_has_the_path_parameters_and_header():
    session = FakeSession(Answer(200, [row("2026-10-28", 500.0)]))
    daily(client(session), "BRK.B")
    (call,) = session.calls
    assert call.url == "https://api.tiingo.com/tiingo/daily/BRK-B/prices"
    assert call.params == {"startDate": "2026-10-21", "endDate": "2026-10-28", "format": "json", "resampleFreq": "daily"}
    assert call.headers == {"Authorization": f"Token {KEY}"}
    assert KEY not in call.url and KEY not in str(call.params)


def test_prices_become_raw_bars_and_actions():
    rows = [
        row("2026-10-23", 49.0, div=0.5, split=2.0, volume=3_000_000),
        row("2026-10-21", 100.0),
        row("2026-10-22", 101.0, div=1.25),
        row("2026-10-26", 30.0, split=0.1),
    ]
    bars, actions = translate_prices(rows)
    days = [to_day(date(2026, 10, d)) for d in (21, 22, 23, 26)]
    assert_array_equal(bars.day, days)
    assert_array_equal(bars.close, [100.0, 101.0, 49.0, 30.0])
    assert_array_equal(bars.open, [99.0, 100.0, 48.0, 29.0])
    assert_array_equal(bars.high, [102.0, 103.0, 51.0, 32.0])
    assert_array_equal(bars.low, [98.0, 99.0, 47.0, 28.0])
    assert_array_equal(bars.volume, [1e6, 1e6, 3e6, 1e6])
    assert not bars.no_trade.any() and bars.day.dtype == np.int64 and bars.no_trade.dtype == bool
    assert actions == [
        Action(days[1], "CASH_DIV", "DIV_RESULT", cash=1.25),
        Action(days[2], "CASH_DIV", "DIV_RESULT", cash=0.5),
        Action(days[2], "SHARE_RATIO", "SPLIT", ratio=2.0),
        Action(days[3], "SHARE_RATIO", "REVERSE_SPLIT", ratio=0.1),
    ]


def test_an_empty_answer_gives_empty_bars():
    bars, actions = translate_prices([])
    assert len(bars.day) == 0 and bars.day.dtype == np.int64 and bars.close.dtype == np.float64 and actions == []


@pytest.mark.parametrize("missing", ["date", "open", "high", "low", "close", "volume", "divCash", "splitFactor"])
def test_a_missing_field_is_a_bad_payload(missing):
    broken = row("2026-10-22", 10.0)
    del broken[missing]
    with pytest.raises(SourceError) as caught:
        translate_prices([row("2026-10-21", 10.0), broken])
    assert str(caught.value) == f"BAD_PAYLOAD: Tiingo's prices, row 2 has no field {missing}"


def test_an_answer_that_is_not_a_list_is_a_bad_payload():
    with pytest.raises(SourceError) as caught:
        daily(client(FakeSession(Answer(200, {"detail": "x"}))))
    assert caught.value.code == "BAD_PAYLOAD"


def test_an_unknown_ticker_is_symbol_unknown_without_a_retry():
    time = FakeTime()
    session = FakeSession(Answer(404, {"detail": "Error: Ticker 'XYZ' not found"}))
    with pytest.raises(SourceError) as caught:
        daily(client(session, time=time), "XYZ")
    assert str(caught.value) == "SYMBOL_UNKNOWN: Tiingo does not know the ticker XYZ"
    assert len(session.calls) == 1 and time.sleeps == []


def test_an_empty_key_fails_without_a_request():
    session = FakeSession()
    budget = RequestBudget(tiingo.LIMITS)
    with pytest.raises(SourceError) as caught:
        daily(client(session, key="", budget=budget))
    assert caught.value.code == "MISSING_KEY"
    assert session.calls == [] and budget.times == []


def test_the_budget_allows_45_an_hour_and_900_a_day():
    budget = RequestBudget(tiingo.LIMITS)
    moments = []
    now = 0.0
    for _ in range(901):
        now += budget.wait(now)
        budget.record(now)
        moments.append(now)
    assert moments[44] == 0 and moments[45] == 3600
    assert moments[899] == 19 * 3600
    assert moments[900] == 86400


def test_the_46th_request_of_an_hour_waits():
    time = FakeTime()
    budget = RequestBudget(tiingo.LIMITS, [time.now - 60] * 45)
    session = FakeSession(Answer(200, [row("2026-10-28", 500.0)]))
    daily(client(session, time=time, budget=budget))
    assert time.sleeps == [3540.0] and len(session.calls) == 1


def test_errors_and_log_lines_hold_no_key_or_url(caplog):
    failures = [
        [Answer(404)],
        [Answer(401, {"detail": f"Invalid token {KEY}"})],
        [TimeoutError()] * 4,
        [aiohttp.ClientConnectionError(f"https://api.tiingo.com/?token={KEY}")] * 4,
        [Answer(500)] * 4,
        [Answer(429, headers={"Retry-After": "1"})] * 2,
        [Answer(200, b"not json")],
    ]
    texts = []
    with caplog.at_level(logging.DEBUG, logger="btlab"):
        for script in failures:
            with pytest.raises(SourceError) as caught:
                daily(client(FakeSession(*script)), "BRK.B")
            texts.append(str(caught.value))
    texts += [record.getMessage() for record in caplog.records]
    assert len(texts) > len(failures)
    for text in texts:
        assert KEY not in text and "://" not in text, text
