"""The request rules shared by the HTTP clients (SPEC.md §5.7, AC-3)."""

import asyncio
import functools
import logging
from datetime import date

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from btlab import finmind, fred, tiingo
from btlab.http import (
    RequestBudget,
    Requester,
    SourceError,
    day,
    fetch_start,
    field,
    number,
    redact,
)
from btlab.records import to_day
from btlab.store import Store
from fakes import Answer, FakeSession, FakeTime

URL = "https://api.example.invalid/v1/data"
SECRET = "Sentinel-Secret-7f3a9c"


def requester(session, time=None, **options) -> Requester:
    time = time or FakeTime()
    return Requester("Example", session, sleep=time.sleep, clock=time.clock, **options)


def get(requester: Requester, **options):
    params = {"series_id": "X", "api_key": SECRET}
    return asyncio.run(requester.get_json("X from 2026-10-01", URL, params, {"Authorization": f"Token {SECRET}"}, **options))


def test_a_source_error_reads_code_and_text():
    error = SourceError("SYMBOL_UNKNOWN", "Tiingo does not know the ticker XYZ")
    assert (error.code, error.text) == ("SYMBOL_UNKNOWN", "Tiingo does not know the ticker XYZ")
    assert str(error) == "SYMBOL_UNKNOWN: Tiingo does not know the ticker XYZ"


@pytest.mark.parametrize("code", ["NOT_FOUND", "HTTP_", "HTTP_4x4"])
def test_a_source_error_takes_only_the_codes_of_section_5_7(code):
    with pytest.raises(ValueError):
        SourceError(code, "text")


def test_a_source_error_never_holds_a_url_or_a_secret():
    error = SourceError("HTTP_402", f"Requests reach the upper limit. https://finmindtrade.com/x?token={SECRET} token={SECRET}")
    assert "://" not in str(error) and SECRET not in str(error)
    assert str(error).startswith("HTTP_402: Requests reach the upper limit. <link> token=***")


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"{URL}?series_id=DTB3&api_key={SECRET}&file_type=json", f"{URL}?series_id=DTB3&api_key=***&file_type=json"),
        (f"{{'Authorization': 'Token {SECRET}'}}", "{'Authorization': 'Token ***'}"),
        (f"Authorization: Bearer {SECRET}", "Authorization: Bearer ***"),
        (f'{{"token": "{SECRET}", "x": 1}}', '{"token": "***", "x": 1}'),
        (f"login token={SECRET}&next=/", "login token=***&next=/"),
        (f"APIKEY={SECRET}", "APIKEY=***"),
        (f"access_token: {SECRET}", "access_token: ***"),
        ("web.token is still the placeholder", "web.token is still the placeholder"),
        ("MISSING_KEY: the FinMind token is empty", "MISSING_KEY: the FinMind token is empty"),
    ],
)
def test_redact_masks_keys_tokens_and_authorization(text, expected):
    assert redact(text) == expected


def test_a_budget_admits_requests_until_a_window_is_full():
    budget = RequestBudget([(3600.0, 3)])
    for moment in (0.0, 10.0, 20.0):
        assert budget.wait(moment) == 0
        budget.record(moment)
    assert budget.wait(30.0) == 3570.0
    assert budget.wait(3599.0) == 1.0
    assert budget.wait(3600.0) == 0


def test_every_window_of_a_budget_counts():
    budget = RequestBudget([(10.0, 2), (100.0, 3)])
    budget.record(0.0)
    budget.record(1.0)
    assert budget.wait(2.0) == 8.0
    budget.record(11.0)
    assert budget.wait(12.0) == 88.0
    assert budget.wait(100.0) == 0


def test_a_budget_keeps_only_the_longest_window_and_reports_its_log():
    saved = []
    budget = RequestBudget([(10.0, 5), (100.0, 50)], times=[5.0, 1.0, 50.0], on_record=saved.append)
    budget.record(120.0)
    assert budget.times == [50.0, 120.0]
    assert saved == [[50.0, 120.0]]


def test_a_restart_reloads_the_budget_from_the_store(tmp_path):
    store = Store(tmp_path)
    budget = RequestBudget([(3600.0, 45)], store.request_log("tiingo"), functools.partial(store.save_request_log, "tiingo"))
    for second in range(45):
        budget.record(1_000_000.0 + second)
    store.close()
    reopened = Store(tmp_path)
    restarted = RequestBudget([(3600.0, 45)], reopened.request_log("tiingo"))
    assert restarted.wait(1_000_100.0) == 3500.0
    assert restarted.times == budget.times
    reopened.close()


def test_a_request_carries_its_parameters_headers_and_timeout():
    session = FakeSession(Answer(200, {"ok": [1, 2]}))
    assert get(requester(session)) == {"ok": [1, 2]}
    (call,) = session.calls
    assert call.url == URL
    assert call.params == {"series_id": "X", "api_key": SECRET}
    assert call.headers == {"Authorization": f"Token {SECRET}"}
    assert isinstance(call.timeout, aiohttp.ClientTimeout) and call.timeout.total == 30


@pytest.mark.parametrize(
    "failure, code",
    [
        (TimeoutError(), "TIMEOUT"),
        (aiohttp.ServerTimeoutError(), "TIMEOUT"),
        (aiohttp.ClientConnectionError(), "UNREACHABLE"),
        (Answer(503), "HTTP_503"),
        (Answer(500, b"oops"), "HTTP_500"),
    ],
)
def test_transient_failures_get_three_retries_after_2_4_and_8_seconds(failure, code):
    time = FakeTime()
    session = FakeSession(failure, failure, failure, Answer(200, [7]))
    assert get(requester(session, time)) == [7]
    assert time.sleeps == [2.0, 4.0, 8.0]
    time = FakeTime()
    session = FakeSession(failure, failure, failure, failure)
    with pytest.raises(SourceError) as caught:
        get(requester(session, time))
    assert caught.value.code == code
    assert len(session.calls) == 4 and time.sleeps == [2.0, 4.0, 8.0]


def test_retries_are_shared_by_every_transient_failure():
    time = FakeTime()
    session = FakeSession(TimeoutError(), Answer(502), aiohttp.ClientOSError(), Answer(504))
    with pytest.raises(SourceError) as caught:
        get(requester(session, time))
    assert caught.value.code == "HTTP_504"
    assert time.sleeps == [2.0, 4.0, 8.0]


@pytest.mark.parametrize("header, wait", [({"Retry-After": "7"}, 7.0), ({"Retry-After": "600"}, 60.0), ({}, 60.0), ({"Retry-After": "soon"}, 60.0)])
def test_429_waits_retry_after_and_retries_once(header, wait):
    time = FakeTime()
    assert get(requester(FakeSession(Answer(429, headers=header), Answer(200, [1])), time)) == [1]
    assert time.sleeps == [wait]
    time = FakeTime()
    session = FakeSession(Answer(429, headers=header), Answer(429, headers=header))
    with pytest.raises(SourceError) as caught:
        get(requester(session, time))
    assert caught.value.code == "RATE_LIMITED"
    assert time.sleeps == [wait] and len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_client_errors_are_not_retried(status):
    time = FakeTime()
    session = FakeSession(Answer(status, {"detail": "no"}))
    with pytest.raises(SourceError) as caught:
        get(requester(session, time))
    assert caught.value.code == f"HTTP_{status}"
    assert str(caught.value) == f"HTTP_{status}: Example answered {status}: X from 2026-10-01"
    assert len(session.calls) == 1 and time.sleeps == []


def test_an_error_body_can_supply_the_text():
    session = FakeSession(Answer(402, {"msg": f"Requests reach the upper limit. https://example.invalid/?token={SECRET}"}))
    with pytest.raises(SourceError) as caught:
        get(requester(session), error_text=lambda payload: payload.get("msg"))
    assert str(caught.value) == "HTTP_402: Example answered 402 (Requests reach the upper limit. <link>): X from 2026-10-01"


def test_a_body_that_is_not_json_is_a_bad_payload():
    with pytest.raises(SourceError) as caught:
        get(requester(FakeSession(Answer(200, b"<html>maintenance</html>"))))
    assert caught.value.code == "BAD_PAYLOAD"


def test_a_spent_budget_waits_with_progress(caplog):
    time = FakeTime()
    waits = []
    budget = RequestBudget([(3600.0, 2)])
    client = requester(FakeSession(*[Answer(200, [n]) for n in range(3)]), time, budget=budget, on_wait=waits.append)
    start = time.now
    with caplog.at_level(logging.INFO, logger="btlab.http"):
        assert [get(client) for _ in range(3)] == [[0], [1], [2]]
    assert time.sleeps == [3600.0]
    assert waits == [start + 3600.0]
    assert budget.times == [start + 3600.0]  # the first two fell out of the hour
    assert "Example: request budget spent; waiting until 2026-10-28T02:00:00Z" in caplog.text


def test_every_attempt_counts_against_the_budget():
    budget = RequestBudget([(3600.0, 10)])
    get(requester(FakeSession(Answer(503), Answer(429), Answer(200, [1])), budget=budget))
    assert len(budget.times) == 3


def test_log_lines_hold_no_url_key_or_token(caplog):
    budget = RequestBudget([(3600.0, 1)])
    script = [TimeoutError(), Answer(503), Answer(429, headers={"Retry-After": "1"}), Answer(200, [1]), Answer(404)]
    client = requester(FakeSession(*script), budget=budget)
    with caplog.at_level(logging.DEBUG, logger="btlab"):
        get(client)
        with pytest.raises(SourceError) as caught:
            get(client)
    assert caplog.records
    for text in [record.getMessage() for record in caplog.records] + [str(caught.value)]:
        assert "://" not in text and SECRET not in text and "api_key" not in text, text


@pytest.mark.parametrize(
    "data_start, last, overlap, expected",
    [
        (9000, None, tiingo.PRICE_OVERLAP_DAYS, 9000),
        (9000, 20000, tiingo.PRICE_OVERLAP_DAYS, 19993),
        (9000, 20000, finmind.PRICE_OVERLAP_DAYS, 19993),
        (9000, None, finmind.ACTION_OVERLAP_DAYS, 9000),
        (9000, 20000, finmind.ACTION_OVERLAP_DAYS, 19600),
        (9000, 9200, finmind.ACTION_OVERLAP_DAYS, 9000),
        (9000, 20000, fred.OVERLAP_DAYS, 19970),
    ],
)
def test_fetch_windows_start_at_data_start_then_overlap_the_latest_day(data_start, last, overlap, expected):
    assert fetch_start(data_start, last, overlap) == expected


def test_payload_helpers_name_the_missing_field():
    row = {"date": "2026-10-28T00:00:00.000Z", "close": 12.5, "flag": True, "text": "x"}
    assert field(row, "close", "row 1") == 12.5
    assert number(row, "close", "row 1") == 12.5
    assert day(row, "date", "row 1") == to_day(date(2026, 10, 28))
    for call, message in [
        (lambda: field(row, "open", "Tiingo's prices, row 3"), "BAD_PAYLOAD: Tiingo's prices, row 3 has no field open"),
        (lambda: field([1], "open", "row 4"), "BAD_PAYLOAD: row 4 has no field open"),
        (lambda: number(row, "flag", "row 1"), "BAD_PAYLOAD: row 1: flag is not a number"),
        (lambda: number(row, "text", "row 1"), "BAD_PAYLOAD: row 1: text is not a number"),
        (lambda: day(row, "text", "row 1"), "BAD_PAYLOAD: row 1: text is not a date"),
        (lambda: day(row, "close", "row 1"), "BAD_PAYLOAD: row 1: close is not a date"),
    ]:
        with pytest.raises(SourceError) as caught:
            call()
        assert str(caught.value) == message


def test_the_rules_hold_through_a_real_aiohttp_session():
    """aiohttp's own client against its test server on 127.0.0.1 (§24.1): no other host is involved."""
    seen = []
    answers = [web.Response(status=503), web.Response(status=429, headers={"Retry-After": "3"})]

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.query), request.headers.get("Authorization")))
        return answers.pop(0) if answers else web.json_response({"rows": [1, 2]})

    async def scenario():
        app = web.Application()
        app.router.add_get("/v1/data", handler)
        time = FakeTime()
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            client = Requester("Example", session, sleep=time.sleep, clock=time.clock)
            payload = await client.get_json("X", str(server.make_url("/v1/data")), {"a": "1", "day": "2026-10-28"}, {"Authorization": "Token k"})
        return payload, time.sleeps

    payload, sleeps = asyncio.run(scenario())
    assert payload == {"rows": [1, 2]}
    assert sleeps == [2.0, 3.0]
    assert seen == [("/v1/data", {"a": "1", "day": "2026-10-28"}, "Token k")] * 3
