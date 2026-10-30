"""Trading days, sessions, periods and the TW limit lock (SPEC.md §4.2, §4.3, §6.5, §9.3). Pure."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np

from btlab.markets import MARKETS
from btlab.records import to_date, to_day

# 1970-01-01 was a Thursday; Monday is 0.
_THURSDAY = 3


def weekday(days: np.ndarray | int) -> np.ndarray:
    """Monday is 0, Sunday 6."""
    return (np.asarray(days, dtype=np.int64) + _THURSDAY) % 7


def trading_days(stored: np.ndarray, through: int) -> np.ndarray:
    """A market's calendar through `through`: the stored days (US: the days of SPY's bars; TW: FinMind's calendar),
    then every weekday after the last stored day, since later holidays are unknown (§4.3, §25)."""
    stored = np.asarray(stored, dtype=np.int64)
    first = int(stored[-1]) + 1 if len(stored) else through + 1
    later = np.arange(first, through + 1, dtype=np.int64)
    return np.concatenate([stored[stored <= through], later[weekday(later) < 5]])


def next_session(stored: np.ndarray, day: int) -> int:
    """The first trading day after `day`, by the rule of trading_days."""
    stored = np.asarray(stored, dtype=np.int64)
    index = np.searchsorted(stored, day, side="right")
    if index < len(stored):
        return int(stored[index])
    following = max(day, int(stored[-1]) if len(stored) else day) + 1
    while weekday(following) >= 5:
        following += 1
    return following


def session(market: str, day: int) -> tuple[int, int]:
    """The regular session of `day` as UTC epoch seconds (§4.2); zoneinfo handles daylight saving."""
    spec = MARKETS[market]
    zone = ZoneInfo(spec.time_zone)
    return _epoch(day, spec.session.open, zone), _epoch(day, spec.session.close, zone)


def session_end(market: str, day: int, last_minute_t: int | None = None) -> int:
    """The end of the day's last stored minute bar, which is earlier on early-close days, or the regular close."""
    return session(market, day)[1] if last_minute_t is None else int(last_minute_t) + 60


def period_starts(days: np.ndarray, frequency: str) -> np.ndarray:
    """True on the first bar and on each bar whose month (monthly) or ISO week (weekly) differs from the previous bar's.

    It needs no knowledge of later trading days, so backtests and live portfolios agree (§9.3).
    """
    days = np.asarray(days, dtype=np.int64)
    if frequency == "monthly":
        key = days.astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)
    elif frequency == "weekly":
        key = days - weekday(days)  # the Monday of the ISO week
    else:
        raise ValueError(f"unknown period {frequency}")
    starts = np.ones(len(days), dtype=bool)
    starts[1:] = key[1:] != key[:-1]
    return starts


def price_limit_pct(market: str, days: np.ndarray) -> np.ndarray:
    """The daily price limit in percent on each day; NaN where the market has none (§4.1)."""
    days = np.asarray(days, dtype=np.int64)
    limits = np.full(len(days), np.nan)
    for limit in MARKETS[market].price_limits:
        limits[days >= to_day(limit.start)] = limit.pct
    return limits


def limit_lock(
    market: str, days: np.ndarray, open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, ref: np.ndarray
) -> np.ndarray:
    """+1 locked up, −1 locked down, 0 otherwise, for bars × symbols arrays (§6.5).

    A bar is locked when open = high = low = close and |close / ref − 1| ≥ 0.9 × the limit. The 0.9 margin absorbs tick
    rounding of the limit price. Locked up blocks buys and covers; locked down blocks sells and short sales.
    """
    threshold = 0.9 * price_limit_pct(market, days)[:, None] / 100
    flat = (open_ == high) & (high == low) & (low == close)
    with np.errstate(divide="ignore", invalid="ignore"):
        move = close / ref - 1
    lock = np.zeros(close.shape, dtype=np.int8)
    lock[flat & (move >= threshold)] = 1
    lock[flat & (move <= -threshold)] = -1
    return lock


def _epoch(day: int, clock: time, zone: ZoneInfo) -> int:
    return int(datetime.combine(to_date(day), clock, tzinfo=zone).timestamp())
