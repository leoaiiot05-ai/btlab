"""Trading days, sessions, periods and the TW limit lock (SPEC.md §4.2, §4.3, §6.5, §9.3, AC-5)."""

from datetime import date, datetime, timezone

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.calendar import (
    limit_lock,
    next_session,
    period_starts,
    price_limit_pct,
    session,
    session_end,
    trading_days,
    weekday,
)
from btlab.records import to_day


def d(iso: str) -> int:
    return to_day(date.fromisoformat(iso))


def days(*isos: str) -> np.ndarray:
    return np.array([d(iso) for iso in isos], dtype=np.int64)


def utc(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp())


def test_weekdays_count_from_a_thursday():
    assert weekday(0) == 3
    assert_array_equal(weekday(days("2026-10-26", "2026-10-30", "2026-11-01")), [0, 4, 6])


def test_stored_days_then_weekdays_make_the_calendar():
    # FinMind's TW calendar, or the days of SPY's bars: Wednesday 2026-10-28 is a holiday.
    stored = days("2026-10-26", "2026-10-27", "2026-10-29")
    calendar = trading_days(stored, d("2026-11-03"))
    assert_array_equal(calendar, days("2026-10-26", "2026-10-27", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"))
    assert calendar.dtype == np.int64
    assert_array_equal(trading_days(stored, d("2026-10-27")), days("2026-10-26", "2026-10-27"))
    assert len(trading_days(np.array([], dtype=np.int64), d("2026-10-27"))) == 0


@pytest.mark.parametrize(
    "day, expected",
    [("2026-10-27", "2026-10-29"), ("2026-10-28", "2026-10-29"), ("2026-10-29", "2026-10-30"), ("2026-10-30", "2026-11-02"),
     ("2026-10-31", "2026-11-02"), ("2026-11-02", "2026-11-03")],
)
def test_the_next_session_skips_holidays_and_weekends(day, expected):
    stored = days("2026-10-26", "2026-10-27", "2026-10-29")
    assert next_session(stored, d(day)) == d(expected)


def test_the_next_session_without_a_stored_calendar_is_the_next_weekday():
    assert next_session(np.array([], dtype=np.int64), d("2026-10-30")) == d("2026-11-02")


@pytest.mark.parametrize(
    "market, iso, start, end",
    [
        ("US", "2026-03-06", utc(2026, 3, 6, 14, 30), utc(2026, 3, 6, 21, 0)),  # EST
        ("US", "2026-03-09", utc(2026, 3, 9, 13, 30), utc(2026, 3, 9, 20, 0)),  # EDT from 2026-03-08
        ("US", "2026-10-30", utc(2026, 10, 30, 13, 30), utc(2026, 10, 30, 20, 0)),
        ("US", "2026-11-02", utc(2026, 11, 2, 14, 30), utc(2026, 11, 2, 21, 0)),  # EST from 2026-11-01
        ("TW", "2026-03-09", utc(2026, 3, 9, 1, 0), utc(2026, 3, 9, 5, 30)),
        ("TW", "2026-11-02", utc(2026, 11, 2, 1, 0), utc(2026, 11, 2, 5, 30)),
    ],
)
def test_sessions_in_utc_across_daylight_saving(market, iso, start, end):
    assert session(market, d(iso)) == (start, end)
    assert session_end(market, d(iso)) == end


def test_an_early_close_ends_with_the_last_minute_bar():
    # The day after Thanksgiving closes at 13:00 New York; the last bar starts at 12:59.
    assert session_end("US", d("2026-11-27"), last_minute_t=utc(2026, 11, 27, 17, 59)) == utc(2026, 11, 27, 18, 0)


def test_periods_start_on_a_new_month_or_iso_week():
    bars = days("2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-04", "2027-01-05", "2027-02-01")
    assert_array_equal(period_starts(bars, "monthly"), [True, False, False, True, False, False, True])
    # 2026-12-28 to 2027-01-03 is ISO week 53 of 2026.
    assert_array_equal(period_starts(bars, "weekly"), [True, False, False, False, True, False, True])
    assert_array_equal(period_starts(days("2026-10-30"), "weekly"), [True])
    with pytest.raises(ValueError):
        period_starts(bars, "quarterly")


def test_price_limits_by_date():
    assert_array_equal(price_limit_pct("TW", days("2015-05-29", "2015-06-01", "2026-10-30")), [7.0, 10.0, 10.0])
    assert np.isnan(price_limit_pct("US", days("2026-10-30"))).all()


def lock_of(market: str, iso: str, bar: tuple[float, float, float, float], ref: float) -> int:
    o, h, l, c = (np.array([[value]]) for value in bar)
    return int(limit_lock(market, days(iso), o, h, l, c, np.array([[ref]]))[0, 0])


@pytest.mark.parametrize(
    "iso, bar, ref, locked",
    [
        ("2015-05-29", (107.0,) * 4, 100.0, 1),
        ("2015-05-29", (106.4,) * 4, 100.0, 1),  # above 0.9 × 7%
        ("2015-05-29", (106.2,) * 4, 100.0, 0),
        ("2015-05-29", (93.6,) * 4, 100.0, -1),
        ("2015-05-29", (93.8,) * 4, 100.0, 0),
        ("2015-06-01", (106.4,) * 4, 100.0, 0),  # 10% from 2015-06-01
        ("2015-06-01", (109.1,) * 4, 100.0, 1),
        ("2015-06-01", (108.9,) * 4, 100.0, 0),
        ("2015-06-01", (90.9,) * 4, 100.0, -1),
        ("2015-06-01", (91.1,) * 4, 100.0, 0),
        ("2015-06-01", (110.0, 110.0, 109.5, 110.0), 100.0, 0),  # traded below the limit: not flat
        ("2015-06-01", (110.0,) * 4, np.nan, 0),
        ("2015-06-01", (np.nan,) * 4, 100.0, 0),
    ],
)
def test_the_tw_limit_lock(iso, bar, ref, locked):
    assert lock_of("TW", iso, bar, ref) == locked


def test_us_bars_never_lock():
    assert lock_of("US", "2026-10-30", (130.0,) * 4, 100.0) == 0
