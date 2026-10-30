"""Tiingo: US daily bars and corporate actions (SPEC.md §5.1)."""

import numpy as np

from btlab.http import Requester, SourceError, day, number, rows_of
from btlab.records import Action, DailyBars, to_date

URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
# The free plan allows 50 an hour and 1,000 a day; the client keeps a margin.
LIMITS = ((3600.0, 45), (86400.0, 900))
PRICE_OVERLAP_DAYS = 7


def tiingo_ticker(symbol: str) -> str:
    """The owner writes class shares with a dot (BRK.B); Tiingo receives a hyphen (BRK-B)."""
    return symbol.replace(".", "-")


class TiingoClient:
    def __init__(self, requester: Requester, api_key: str) -> None:
        self._requester = requester
        self._api_key = api_key

    async def daily(self, symbol: str, first_day: int, last_day: int) -> tuple[DailyBars, list[Action]]:
        """Raw bars and the actions of [first_day, last_day]. An unknown ticker is SYMBOL_UNKNOWN."""
        if not self._api_key:
            raise SourceError("MISSING_KEY", "the Tiingo API key is empty")
        ticker = tiingo_ticker(symbol)
        first, last = to_date(first_day).isoformat(), to_date(last_day).isoformat()
        try:
            rows = await self._requester.get_json(
                f"prices of {ticker} from {first} to {last}",
                URL.format(ticker=ticker),
                {"startDate": first, "endDate": last, "format": "json", "resampleFreq": "daily"},
                {"Authorization": f"Token {self._api_key}"},
            )
        except SourceError as error:
            if error.code == "HTTP_404":
                raise SourceError("SYMBOL_UNKNOWN", f"Tiingo does not know the ticker {ticker}") from None
            raise
        return translate_prices(rows)


def translate_prices(payload: object) -> tuple[DailyBars, list[Action]]:
    """Tiingo's rows as raw bars in day order and the actions they announce. The adjusted fields are not used:
    btlab computes its own (§6.3). Whether an action is usable is for prices.py to decide (§6.2)."""
    rows = sorted(_row(row, f"Tiingo's prices, row {index + 1}") for index, row in enumerate(rows_of(payload, "Tiingo's prices")))
    days, open_, high, low, close, volume, cash, ratio = zip(*rows) if rows else [()] * 8
    prices = (np.array(column, dtype=np.float64) for column in (open_, high, low, close, volume))
    bars = DailyBars(np.array(days, dtype=np.int64), *prices, np.zeros(len(rows), dtype=bool))
    actions = []
    for when, dividend, factor in zip(days, cash, ratio):
        if dividend > 0:
            actions.append(Action(when, "CASH_DIV", "DIV_RESULT", cash=dividend))
        if factor != 1:
            actions.append(Action(when, "SHARE_RATIO", "SPLIT" if factor > 1 else "REVERSE_SPLIT", ratio=factor))
    return bars, actions


def _row(row: object, where: str) -> tuple:
    keys = ("open", "high", "low", "close", "volume", "divCash", "splitFactor")
    return (day(row, "date", where), *(number(row, key, where) for key in keys))
