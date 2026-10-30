"""FRED: the 3-month T-bill rate DTB3, the risk-free rate of both markets (SPEC.md §5.3).

FRED takes its key in the query string, so the URL is never logged or put in an error (§21).
"""

import numpy as np

from btlab.http import Requester, SourceError, day, field, rows_of
from btlab.records import RiskFreeRates, to_date

URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES = "DTB3"
OVERLAP_DAYS = 30


class FredClient:
    def __init__(self, requester: Requester, api_key: str) -> None:
        self._requester = requester
        self._api_key = api_key

    async def dtb3(self, first_day: int) -> RiskFreeRates:
        """DTB3 in percent from first_day on; days without a value are absent."""
        if not self._api_key:
            raise SourceError("MISSING_KEY", "the FRED API key is empty")
        start = to_date(first_day).isoformat()
        payload = await self._requester.get_json(
            f"{SERIES} from {start}",
            URL,
            {"series_id": SERIES, "api_key": self._api_key, "file_type": "json", "observation_start": start},
        )
        return translate_observations(payload)


def translate_observations(payload: object) -> RiskFreeRates:
    """FRED marks a missing value with "."; such days are dropped."""
    days, rates = [], []
    for index, row in enumerate(rows_of(field(payload, "observations", "FRED's answer"), "FRED's observations")):
        where = f"FRED's observations, row {index + 1}"
        value = field(row, "value", where)
        if value == ".":
            continue
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise SourceError("BAD_PAYLOAD", f"{where}: value is not a number") from None
        if not np.isfinite(rate):
            raise SourceError("BAD_PAYLOAD", f"{where}: value is not a finite number")
        days.append(day(row, "date", where))
        rates.append(rate)
    order = np.argsort(days, kind="stable")
    return RiskFreeRates(np.array(days, dtype=np.int64)[order], np.array(rates, dtype=np.float64)[order])
