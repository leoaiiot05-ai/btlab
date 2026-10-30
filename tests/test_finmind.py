"""FinMind: requests, maintenance, translation and the budget (SPEC.md §5.2, AC-3)."""

import asyncio
import logging
from datetime import date, datetime

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab import finmind
from btlab.finmind import (
    TAIPEI,
    FinMindClient,
    in_maintenance,
    translate_capital_reductions,
    translate_dividend_policies,
    translate_dividend_results,
    translate_par_value_changes,
    translate_prices,
    translate_short_suspensions,
    translate_split_prices,
    translate_stock_info,
    translate_trading_days,
)
from btlab.http import RequestBudget, Requester, SourceError
from btlab.records import (
    CapitalReduction,
    DividendPolicy,
    DividendResult,
    ParValueChange,
    ShortSuspension,
    SplitPrice,
    StockInfo,
    to_day,
)
from fakes import Answer, FakeSession, FakeTime

TOKEN = "Sentinel-FinMind-Token-9d1a"
FIRST, LAST = to_day(date(2025, 9, 23)), to_day(date(2026, 10, 28))
WEDNESDAY = datetime(2026, 10, 28, 18, 5, tzinfo=TAIPEI)


def ok(rows: list) -> Answer:
    return Answer(200, {"msg": "success", "status": 200, "data": rows})


def client(session, token=TOKEN, moment=WEDNESDAY) -> FinMindClient:
    time = FakeTime(moment)
    return FinMindClient(Requester("FinMind", session, sleep=time.sleep, clock=time.clock), token)


def d(iso: str) -> int:
    return to_day(date.fromisoformat(iso))


REQUESTS = [
    (lambda c: c.trading_days(), {"dataset": "TaiwanStockTradingDate"}),
    (lambda c: c.stock_info(), {"dataset": "TaiwanStockInfo"}),
    (lambda c: c.prices("2330", FIRST, LAST), {"dataset": "TaiwanStockPrice", "data_id": "2330"}),
    (lambda c: c.dividend_results("2330", FIRST, LAST), {"dataset": "TaiwanStockDividendResult", "data_id": "2330"}),
    (lambda c: c.dividend_policies("2330", FIRST, LAST), {"dataset": "TaiwanStockDividend", "data_id": "2330"}),
    (lambda c: c.split_prices(FIRST, LAST), {"dataset": "TaiwanStockSplitPrice"}),
    (lambda c: c.par_value_changes(FIRST), {"dataset": "TaiwanStockParValueChange", "start_date": "2025-09-23"}),
    (
        lambda c: c.capital_reductions("2330", FIRST, LAST),
        {"dataset": "TaiwanStockCapitalReductionReferencePrice", "data_id": "2330"},
    ),
    (
        lambda c: c.short_suspensions("2330", FIRST, LAST),
        {"dataset": "TaiwanStockMarginShortSaleSuspension", "data_id": "2330"},
    ),
]
DATED = {"TaiwanStockPrice", "TaiwanStockDividendResult", "TaiwanStockDividend", "TaiwanStockSplitPrice",
         "TaiwanStockCapitalReductionReferencePrice", "TaiwanStockMarginShortSaleSuspension"}


@pytest.mark.parametrize("call, params", REQUESTS, ids=[params["dataset"] for _, params in REQUESTS])
def test_each_dataset_is_requested_with_its_parameters_and_bearer_token(call, params):
    session = FakeSession(ok([]))
    asyncio.run(call(client(session)))
    (sent,) = session.calls
    expected = dict(params)
    if params["dataset"] in DATED:
        expected |= {"start_date": "2025-09-23", "end_date": "2026-10-28"}
    assert sent.url == "https://api.finmindtrade.com/api/v4/data"
    assert sent.params == expected
    assert sent.headers == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in sent.url and TOKEN not in str(sent.params)


@pytest.mark.parametrize(
    "moment, closed",
    [
        (datetime(2026, 10, 31, 23, 59, 59, tzinfo=TAIPEI), False),
        (datetime(2026, 11, 1, 0, 0, tzinfo=TAIPEI), True),
        (datetime(2026, 11, 1, 6, 59, 59, tzinfo=TAIPEI), True),
        (datetime(2026, 11, 1, 7, 0, tzinfo=TAIPEI), False),
        (datetime(2026, 11, 2, 0, 30, tzinfo=TAIPEI), False),
    ],
)
def test_sunday_morning_is_maintenance_without_a_request(moment, closed):
    assert datetime(2026, 11, 1, tzinfo=TAIPEI).weekday() == 6
    assert in_maintenance(moment.timestamp()) == closed
    session = FakeSession(*([] if closed else [ok([{"date": "2026-11-02"}])]))
    if closed:
        with pytest.raises(SourceError) as caught:
            asyncio.run(client(session, moment=moment).trading_days())
        assert caught.value.code == "MAINTENANCE"
        assert session.calls == []
    else:
        asyncio.run(client(session, moment=moment).trading_days())
        assert len(session.calls) == 1


def test_an_empty_token_fails_without_a_request():
    with pytest.raises(SourceError) as caught:
        asyncio.run(client(FakeSession(), token="").prices("2330", FIRST, LAST))
    assert caught.value.code == "MISSING_KEY"


@pytest.mark.parametrize(
    "answer",
    [
        Answer(200, {"msg": "Requests reach the upper limit. https://finmindtrade.com/", "status": 402}),
        Answer(402, {"msg": "Requests reach the upper limit. https://finmindtrade.com/", "status": 402}),
    ],
    ids=["in the body", "in the HTTP status"],
)
def test_an_error_status_carries_finminds_message(answer):
    with pytest.raises(SourceError) as caught:
        asyncio.run(client(FakeSession(answer)).prices("2330", FIRST, LAST))
    assert str(caught.value) == (
        "HTTP_402: FinMind answered 402 (Requests reach the upper limit. <link>): TaiwanStockPrice 2330 2025-09-23 2026-10-28"
    )


@pytest.mark.parametrize("body", [{"msg": "success", "status": 200}, {"msg": "x", "data": []}, {"status": 200, "data": {}}, [1, 2]])
def test_an_answer_without_its_data_is_a_bad_payload(body):
    with pytest.raises(SourceError) as caught:
        asyncio.run(client(FakeSession(Answer(200, body))).trading_days())
    assert caught.value.code == "BAD_PAYLOAD"


def test_trading_days_are_sorted_days():
    days = translate_trading_days([{"date": "2026-10-28"}, {"date": "2026-10-27"}, {"date": "2026-10-29"}])
    assert_array_equal(days, [d("2026-10-27"), d("2026-10-28"), d("2026-10-29")])
    assert days.dtype == np.int64


def test_stock_info_keeps_each_stocks_latest_row():
    rows = [
        {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積", "type": "twse", "date": "2024-01-02"},
        {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-10-28"},
        {"industry_category": "電子零組件業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "2026-10-28"},
        {"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2026-10-28"},
        {"industry_category": "電子工業", "stock_id": "7777", "stock_name": "興櫃股", "type": "emerging", "date": "2026-10-28"},
        {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "舊名", "type": "twse", "date": "2020-05-05"},
    ]
    assert translate_stock_info(rows) == [
        StockInfo("0050", "元大台灣50", "TWSE", "ETF"),
        StockInfo("2330", "台積電", "TWSE", "半導體業"),
        StockInfo("6488", "環球晶", "TPEX", "電子零組件業"),
        StockInfo("7777", "興櫃股", None, "電子工業"),
    ]


def test_zero_close_rows_are_no_trade_days():
    rows = [
        {"date": "2026-10-28", "stock_id": "2330", "Trading_Volume": 30_000_000, "Trading_money": 1, "open": 1450.0,
         "max": 1465.0, "min": 1440.0, "close": 1460.0, "spread": 10.0, "Trading_turnover": 1},
        {"date": "2026-10-27", "stock_id": "2330", "Trading_Volume": 1200, "Trading_money": 0, "open": 0, "max": 0,
         "min": 0, "close": 0, "spread": 0, "Trading_turnover": 0},
        {"date": "2026-10-24", "stock_id": "2330", "Trading_Volume": 25_000_000, "Trading_money": 1, "open": 1430,
         "max": 1450, "min": 1425, "close": 1445, "spread": 5, "Trading_turnover": 1},
    ]
    bars = translate_prices(rows)
    assert_array_equal(bars.day, [d("2026-10-24"), d("2026-10-27"), d("2026-10-28")])
    assert_array_equal(bars.open, [1430.0, np.nan, 1450.0])
    assert_array_equal(bars.high, [1450.0, np.nan, 1465.0])
    assert_array_equal(bars.low, [1425.0, np.nan, 1440.0])
    assert_array_equal(bars.close, [1445.0, np.nan, 1460.0])
    assert_array_equal(bars.volume, [25e6, 1200.0, 30e6])
    assert_array_equal(bars.no_trade, [False, True, False])
    empty = translate_prices([])
    assert len(empty.day) == 0 and empty.no_trade.dtype == bool


def test_corporate_action_rows_become_typed_records():
    assert translate_dividend_results([
        {"date": "2019-06-24", "stock_id": "2330", "before_price": 248.5, "after_price": 240.5,
         "stock_and_cache_dividend": 8.0, "stock_or_cache_dividend": "息", "max_price": 1, "min_price": 1, "open_price": 1, "reference_price": 1},
    ]) == [DividendResult(d("2019-06-24"), 248.5, 240.5, "息")]
    assert translate_dividend_policies([
        {"date": "2024-05-01", "stock_id": "2330", "CashEarningsDistribution": 3.5, "CashStatutorySurplus": 0.5,
         "CashExDividendTradingDate": "2024-06-13", "StockEarningsDistribution": 0},
        {"date": "2024-05-01", "stock_id": "2330", "CashEarningsDistribution": 0, "CashStatutorySurplus": 0,
         "CashExDividendTradingDate": "", "StockEarningsDistribution": 1.0},
    ]) == [DividendPolicy(d("2024-06-13"), 4.0), DividendPolicy(None, 0.0)]
    assert translate_split_prices([
        {"date": "2024-12-11", "stock_id": "00632R", "type": "反分割", "before_price": 3.28, "after_price": 22.96, "max_price": 1},
    ]) == [SplitPrice(d("2024-12-11"), "00632R", "反分割", 3.28, 22.96)]
    assert translate_par_value_changes([
        {"date": "2022-07-04", "stock_id": "6531", "stock_name": "愛普", "before_close": 900.0, "after_ref_close": 90.0},
    ]) == [ParValueChange(d("2022-07-04"), "6531", 900.0, 90.0)]
    assert translate_capital_reductions([
        {"date": "2021-08-30", "stock_id": "2002", "ClosingPriceonTheLastTradingDay": 30.0,
         "PostReductionReferencePrice": 33.33, "ReasonforCapitalReduction": "退還股款"},
        {"date": "2022-08-30", "stock_id": "2002", "ClosingPriceonTheLastTradingDay": 20.0,
         "PostReductionReferencePrice": 25.0, "ReasonforCapitalReduction": ""},
    ]) == [CapitalReduction(d("2021-08-30"), 30.0, 33.33, "退還股款"), CapitalReduction(d("2022-08-30"), 20.0, 25.0, None)]
    assert translate_short_suspensions([
        {"date": "2026-04-10", "stock_id": "2330", "end_date": "2026-04-16", "reason": "股東常會"},
    ]) == [ShortSuspension(d("2026-04-10"), d("2026-04-16"), "股東常會")]


TRANSLATORS = [
    (translate_trading_days, {"date": "2026-10-28"}),
    (translate_stock_info, {"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "x", "date": "2026-10-28"}),
    (translate_prices, {"date": "2026-10-28", "open": 1.0, "max": 1.0, "min": 1.0, "close": 1.0, "Trading_Volume": 1}),
    (translate_dividend_results, {"date": "2026-10-28", "before_price": 2.0, "after_price": 1.0, "stock_or_cache_dividend": "息"}),
    (translate_dividend_policies, {"CashEarningsDistribution": 1.0, "CashStatutorySurplus": 0.0, "CashExDividendTradingDate": ""}),
    (translate_split_prices, {"date": "2026-10-28", "stock_id": "1", "type": "分割", "before_price": 2.0, "after_price": 1.0}),
    (translate_par_value_changes, {"date": "2026-10-28", "stock_id": "1", "before_close": 2.0, "after_ref_close": 1.0}),
    (translate_capital_reductions, {"date": "2026-10-28", "ClosingPriceonTheLastTradingDay": 2.0,
                                    "PostReductionReferencePrice": 1.0, "ReasonforCapitalReduction": ""}),
    (translate_short_suspensions, {"date": "2026-10-28", "end_date": "2026-10-30", "reason": ""}),
]


@pytest.mark.parametrize("translate, row", TRANSLATORS, ids=[translate.__name__ for translate, _ in TRANSLATORS])
def test_every_needed_field_is_checked(translate, row):
    translate([row])
    for key in row:
        broken = {name: value for name, value in row.items() if name != key}
        with pytest.raises(SourceError) as caught:
            translate([row, broken])
        assert caught.value.code == "BAD_PAYLOAD"
        assert f"row 2 has no field {key}" in str(caught.value)


def test_the_budget_allows_550_an_hour():
    budget = RequestBudget(finmind.LIMITS)
    moments = []
    now = 0.0
    for _ in range(551):
        now += budget.wait(now)
        budget.record(now)
        moments.append(now)
    assert moments[549] == 0 and moments[550] == 3600


def test_errors_and_log_lines_hold_no_token_or_url(caplog):
    failures = [
        [Answer(200, {"msg": f"Your token {TOKEN} is invalid, see https://finmindtrade.com/", "status": 400})],
        [Answer(403, {"msg": f"Authorization: Bearer {TOKEN} refused", "status": 403})],
        [TimeoutError()] * 4,
        [Answer(502)] * 4,
        [Answer(200, {"status": 200, "data": [{"date": "x"}]})],
    ]
    texts = []
    with caplog.at_level(logging.DEBUG, logger="btlab"):
        for script in failures:
            with pytest.raises(SourceError) as caught:
                asyncio.run(client(FakeSession(*script)).trading_days())
            texts.append(str(caught.value))
    texts += [record.getMessage() for record in caplog.records]
    for text in texts:
        assert TOKEN not in text and "://" not in text, text
