"""FRED: the DTB3 request and its translation (SPEC.md §5.3, AC-3)."""

import asyncio
import logging
from datetime import date

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.fred import FredClient, translate_observations
from btlab.http import Requester, SourceError
from btlab.records import to_day
from fakes import Answer, FakeSession, FakeTime

KEY = "Sentinel-Fred-Key-0c4b"
START = to_day(date(2026, 9, 28))


def observation(iso: str, value: str) -> dict:
    return {"realtime_start": "2026-10-28", "realtime_end": "2026-10-28", "date": iso, "value": value}


def client(session, key=KEY) -> FredClient:
    time = FakeTime()
    return FredClient(Requester("FRED", session, sleep=time.sleep, clock=time.clock), key)


def test_the_request_carries_the_key_in_the_query_as_fred_requires():
    session = FakeSession(Answer(200, {"observations": [observation("2026-10-27", "3.95")]}))
    rates = asyncio.run(client(session).dtb3(START))
    (call,) = session.calls
    assert call.url == "https://api.stlouisfed.org/fred/series/observations"
    assert call.params == {"series_id": "DTB3", "api_key": KEY, "file_type": "json", "observation_start": "2026-09-28"}
    assert call.headers == {}
    assert_array_equal(rates.day, [to_day(date(2026, 10, 27))])
    assert_array_equal(rates.rate_pct, [3.95])


def test_missing_values_are_dropped():
    payload = {"observations": [observation("2026-10-23", "3.97"), observation("2026-10-26", "."), observation("2026-10-27", "3.95")]}
    rates = translate_observations(payload)
    assert_array_equal(rates.day, [to_day(date(2026, 10, 23)), to_day(date(2026, 10, 27))])
    assert_array_equal(rates.rate_pct, [3.97, 3.95])
    assert rates.day.dtype == np.int64 and rates.rate_pct.dtype == np.float64
    empty = translate_observations({"observations": []})
    assert len(empty.day) == 0 and empty.day.dtype == np.int64


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"seriess": []}, "BAD_PAYLOAD: FRED's answer has no field observations"),
        ({"observations": {}}, "BAD_PAYLOAD: FRED's observations is not a list"),
        ({"observations": [{"date": "2026-10-27"}]}, "BAD_PAYLOAD: FRED's observations, row 1 has no field value"),
        ({"observations": [observation("2026-10-27", "n/a")]}, "BAD_PAYLOAD: FRED's observations, row 1: value is not a number"),
        ({"observations": [observation("2026-10-27", "inf")]}, "BAD_PAYLOAD: FRED's observations, row 1: value is not a finite number"),
        ({"observations": [observation("27/10/2026", "3.9")]}, "BAD_PAYLOAD: FRED's observations, row 1: date is not a date"),
    ],
)
def test_bad_payloads(payload, message):
    with pytest.raises(SourceError) as caught:
        translate_observations(payload)
    assert str(caught.value) == message


def test_an_empty_key_fails_without_a_request():
    with pytest.raises(SourceError) as caught:
        asyncio.run(client(FakeSession(), key="").dtb3(START))
    assert caught.value.code == "MISSING_KEY"


def test_errors_and_log_lines_hold_no_key_or_url(caplog):
    failures = [[Answer(400, {"error_message": f"Bad Request. api_key={KEY}"})], [TimeoutError()] * 4, [Answer(503)] * 4]
    texts = []
    with caplog.at_level(logging.DEBUG, logger="btlab"):
        for script in failures:
            with pytest.raises(SourceError) as caught:
                asyncio.run(client(FakeSession(*script)).dtb3(START))
            texts.append(str(caught.value))
    texts += [record.getMessage() for record in caplog.records]
    for text in texts:
        assert KEY not in text and "://" not in text and "api_key" not in text, text
