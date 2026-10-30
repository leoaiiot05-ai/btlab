"""FinMind: Taiwan trading days, stock info, daily bars and corporate-action rows (SPEC.md §5.2).

FinMind's data are for non-commercial use, and the UI credits "FinMind".
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from btlab.http import Requester, SourceError, day, field, number, rows_of
from btlab.records import (
    CapitalReduction,
    DailyBars,
    DividendPolicy,
    DividendResult,
    ParValueChange,
    ShortSuspension,
    SplitPrice,
    StockInfo,
    to_date,
)

URL = "https://api.finmindtrade.com/api/v4/data"
# A registered free account allows 600 an hour; the client keeps a margin.
LIMITS = ((3600.0, 550),)
PRICE_OVERLAP_DAYS = 7
# Dividend policies and corrections arrive months ahead.
ACTION_OVERLAP_DAYS = 400
TAIPEI = ZoneInfo("Asia/Taipei")
EXCHANGES = {"twse": "TWSE", "tpex": "TPEX"}


def in_maintenance(now: float) -> bool:
    """FinMind's maintenance window: Sundays 00:00 to 07:00 Asia/Taipei."""
    local = datetime.fromtimestamp(now, TAIPEI)
    return local.weekday() == 6 and local.hour < 7


class FinMindClient:
    def __init__(self, requester: Requester, token: str) -> None:
        self._requester = requester
        self._token = token

    async def trading_days(self) -> np.ndarray:
        return translate_trading_days(await self._data("TaiwanStockTradingDate"))

    async def stock_info(self) -> list[StockInfo]:
        return translate_stock_info(await self._data("TaiwanStockInfo"))

    async def prices(self, symbol: str, first_day: int, last_day: int) -> DailyBars:
        return translate_prices(await self._data("TaiwanStockPrice", symbol, first_day, last_day))

    async def dividend_results(self, symbol: str, first_day: int, last_day: int) -> list[DividendResult]:
        return translate_dividend_results(await self._data("TaiwanStockDividendResult", symbol, first_day, last_day))

    async def dividend_policies(self, symbol: str, first_day: int, last_day: int) -> list[DividendPolicy]:
        return translate_dividend_policies(await self._data("TaiwanStockDividend", symbol, first_day, last_day))

    async def split_prices(self, first_day: int, last_day: int) -> list[SplitPrice]:
        """Every stock's splits; the update keeps those of its symbols."""
        return translate_split_prices(await self._data("TaiwanStockSplitPrice", None, first_day, last_day))

    async def par_value_changes(self, first_day: int) -> list[ParValueChange]:
        """Every stock's par-value changes since first_day; the dataset takes no end date."""
        return translate_par_value_changes(await self._data("TaiwanStockParValueChange", None, first_day))

    async def capital_reductions(self, symbol: str, first_day: int, last_day: int) -> list[CapitalReduction]:
        dataset = "TaiwanStockCapitalReductionReferencePrice"
        return translate_capital_reductions(await self._data(dataset, symbol, first_day, last_day))

    async def short_suspensions(self, symbol: str, first_day: int, last_day: int) -> list[ShortSuspension]:
        dataset = "TaiwanStockMarginShortSaleSuspension"
        return translate_short_suspensions(await self._data(dataset, symbol, first_day, last_day))

    async def _data(
        self, dataset: str, data_id: str | None = None, first_day: int | None = None, last_day: int | None = None
    ) -> list:
        if not self._token:
            raise SourceError("MISSING_KEY", "the FinMind token is empty")
        if in_maintenance(self._requester.clock()):
            raise SourceError("MAINTENANCE", "FinMind is under maintenance on Sundays from 00:00 to 07:00 Asia/Taipei")
        params = {"dataset": dataset}
        if data_id is not None:
            params["data_id"] = data_id
        if first_day is not None:
            params["start_date"] = to_date(first_day).isoformat()
        if last_day is not None:
            params["end_date"] = to_date(last_day).isoformat()
        label = " ".join([dataset, *(params[key] for key in ("data_id", "start_date", "end_date") if key in params)])
        payload = await self._requester.get_json(
            label, URL, params, {"Authorization": f"Bearer {self._token}"}, error_text=self._message
        )
        status = field(payload, "status", f"FinMind's {dataset}")
        if status != 200:
            raise SourceError(f"HTTP_{status}", f"FinMind answered {status} ({self._message(payload)}): {label}")
        return rows_of(field(payload, "data", f"FinMind's {dataset}"), f"FinMind's {dataset} data")

    def _message(self, payload: Any) -> str | None:
        """FinMind's msg, without the token should it ever quote it."""
        message = payload.get("msg") if isinstance(payload, dict) else None
        return message.replace(self._token, "***") if isinstance(message, str) else None


def _numbered(rows: list, dataset: str) -> list[tuple[str, Any]]:
    return [(f"FinMind's {dataset}, row {index + 1}", row) for index, row in enumerate(rows)]


def _text(row: Any, key: str, where: str) -> str:
    value = field(row, key, where)
    if not isinstance(value, str):
        raise SourceError("BAD_PAYLOAD", f"{where}: {key} is not text")
    return value


def _optional_text(row: Any, key: str, where: str) -> str | None:
    value = field(row, key, where)
    return value if isinstance(value, str) and value else None


def translate_trading_days(rows: list) -> np.ndarray:
    return np.array(sorted(day(row, "date", where) for where, row in _numbered(rows, "TaiwanStockTradingDate")), dtype=np.int64)


def translate_stock_info(rows: list) -> list[StockInfo]:
    """Each stock's latest row, by its date, sorted by code."""
    latest: dict[str, tuple[str, StockInfo]] = {}
    for where, row in _numbered(rows, "TaiwanStockInfo"):
        symbol = _text(row, "stock_id", where)
        info = StockInfo(
            symbol=symbol,
            name=_text(row, "stock_name", where),
            exchange=EXCHANGES.get(_text(row, "type", where)),
            industry=_text(row, "industry_category", where),
        )
        when = str(field(row, "date", where))
        if symbol not in latest or when >= latest[symbol][0]:
            latest[symbol] = (when, info)
    return [latest[symbol][1] for symbol in sorted(latest)]


def translate_prices(rows: list) -> DailyBars:
    """Raw bars in day order. A row with close 0 is a no-trade day: NaN prices and the reported volume (§6.1)."""
    bars = []
    for where, row in _numbered(rows, "TaiwanStockPrice"):
        prices = [number(row, key, where) for key in ("open", "max", "min", "close")]
        no_trade = prices[3] == 0
        if no_trade:
            prices = [np.nan] * 4
        bars.append((day(row, "date", where), *prices, number(row, "Trading_Volume", where), no_trade))
    bars.sort(key=lambda bar: bar[0])
    days, open_, high, low, close, volume, no_trade = zip(*bars) if bars else [()] * 7
    prices = (np.array(column, dtype=np.float64) for column in (open_, high, low, close, volume))
    return DailyBars(np.array(days, dtype=np.int64), *prices, np.array(no_trade, dtype=bool))


def translate_dividend_results(rows: list) -> list[DividendResult]:
    return [
        DividendResult(
            day=day(row, "date", where),
            before_price=number(row, "before_price", where),
            after_price=number(row, "after_price", where),
            kind=_text(row, "stock_or_cache_dividend", where),
        )
        for where, row in _numbered(rows, "TaiwanStockDividendResult")
    ]


def translate_dividend_policies(rows: list) -> list[DividendPolicy]:
    """One record per row; D on an ex-date sums the rows of that date (§6.2), which prices.py does."""
    policies = []
    for where, row in _numbered(rows, "TaiwanStockDividend"):
        cash = number(row, "CashEarningsDistribution", where) + number(row, "CashStatutorySurplus", where)
        ex_date = _optional_text(row, "CashExDividendTradingDate", where)
        policies.append(DividendPolicy(None if ex_date is None else day(row, "CashExDividendTradingDate", where), cash))
    return policies


def translate_split_prices(rows: list) -> list[SplitPrice]:
    return [
        SplitPrice(
            day=day(row, "date", where),
            symbol=_text(row, "stock_id", where),
            type=_text(row, "type", where),
            before_price=number(row, "before_price", where),
            after_price=number(row, "after_price", where),
        )
        for where, row in _numbered(rows, "TaiwanStockSplitPrice")
    ]


def translate_par_value_changes(rows: list) -> list[ParValueChange]:
    return [
        ParValueChange(
            day=day(row, "date", where),
            symbol=_text(row, "stock_id", where),
            before_close=number(row, "before_close", where),
            after_ref_close=number(row, "after_ref_close", where),
        )
        for where, row in _numbered(rows, "TaiwanStockParValueChange")
    ]


def translate_capital_reductions(rows: list) -> list[CapitalReduction]:
    return [
        CapitalReduction(
            day=day(row, "date", where),
            closing_price=number(row, "ClosingPriceonTheLastTradingDay", where),
            reference_price=number(row, "PostReductionReferencePrice", where),
            reason=_optional_text(row, "ReasonforCapitalReduction", where),
        )
        for where, row in _numbered(rows, "TaiwanStockCapitalReductionReferencePrice")
    ]


def translate_short_suspensions(rows: list) -> list[ShortSuspension]:
    return [
        ShortSuspension(
            start_day=day(row, "date", where),
            end_day=day(row, "end_date", where),
            reason=_optional_text(row, "reason", where),
        )
        for where, row in _numbered(rows, "TaiwanStockMarginShortSaleSuspension")
    ]
