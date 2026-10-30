"""Minute-derived daily features, today-so-far values and daily values on minute timelines (SPEC.md §8.2–§8.4, AC-6)."""

import math
from datetime import date, datetime, timezone

import numpy as np
from numpy.testing import assert_allclose, assert_array_equal

from btlab import features
from btlab.indicators import Inputs
from btlab.records import MinuteBars, to_day

NAN = math.nan
MON, TUE, WED = (to_day(date(2026, 10, d)) for d in (26, 27, 28))
EARLY = to_day(date(2026, 11, 27))  # the day after Thanksgiving closes at 13:00 New York


def utc(day: int, hour: int, minute: int) -> int:
    on = date.fromordinal(date(1970, 1, 1).toordinal() + day)
    return int(datetime(on.year, on.month, on.day, hour, minute, tzinfo=timezone.utc).timestamp())


def minute_bars(rows: list[tuple[int, float, float, float, float, float]]) -> MinuteBars:
    t, o, h, l, c, v = (np.array(column) for column in zip(*rows))
    return MinuteBars(t.astype(np.int64), o, h, l, c, v)


# Monday: the open bar, the bar that ends 30 minutes after the open, the one that ends 30 minutes before the close, the close.
MONDAY = [
    (utc(MON, 13, 30), 100.0, 101.0, 99.0, 100.5, 1000.0),
    (utc(MON, 13, 59), 101.5, 102.5, 101.0, 102.0, 500.0),
    (utc(MON, 19, 29), 103.5, 104.5, 103.0, 104.0, 700.0),
    (utc(MON, 19, 59), 104.0, 105.5, 103.0, 105.0, 2000.0),
]
WEDNESDAY = [(utc(WED, 13, 30), 50.0, 51.0, 48.0, 49.0, 100.0), (utc(WED, 19, 59), 49.0, 52.0, 49.0, 51.0, 100.0)]


def sessions(days, rows, covered, multiplier=None) -> features.SessionBars:
    multiplier = np.ones(len(days)) if multiplier is None else np.array(multiplier, dtype=float)
    return features.session_bars("US", np.array(days), minute_bars(rows), np.array(covered), multiplier)


def test_session_bars_cut_minute_bars_into_covered_sessions():
    s = sessions([MON, TUE, WED], MONDAY + WEDNESDAY, [MON, WED])
    assert_array_equal(s.start, [0, 0, 4])
    assert_array_equal(s.stop, [4, 0, 6])
    assert s.open_time[0] == utc(MON, 13, 30) and s.end_time[0] == utc(MON, 20, 0)


def test_the_features_of_a_session():
    s = sessions([MON, TUE, WED], MONDAY + WEDNESDAY, [MON, WED])
    # Wednesday has no bar between the open and the close: 30 minutes before the close, the open bar is the latest.
    assert_allclose(features.last_n_min_return(s, 30), [105 / 104 - 1, NAN, 51 / 49 - 1], equal_nan=True)
    assert_allclose(features.first_n_min_return(s, 30), [102 / 100 - 1, NAN, 49 / 50 - 1], equal_nan=True)
    # A missing minute takes the latest earlier bar: the 13:59 bar is still the latest that ends by 14:01.
    assert_allclose(features.first_n_min_return(s, 31)[0], 102 / 100 - 1)
    typical = [(h + l + c) / 3 for _, _, h, l, c, _ in MONDAY]
    vwap = sum(tp * row[5] for tp, row in zip(typical, MONDAY)) / sum(row[5] for row in MONDAY)
    assert_allclose(features.close_vs_vwap(s)[0], 105 / vwap - 1)
    assert_allclose(features.range_position(s), [(105 - 99) / (105.5 - 99), NAN, (51 - 48) / (52 - 48)], equal_nan=True)


def test_a_session_whose_high_equals_its_low_sits_in_the_middle():
    flat = [(utc(MON, 13, 30), 10.0, 10.0, 10.0, 10.0, 5.0)]
    assert_allclose(features.range_position(sessions([MON], flat, [MON])), [0.5])


def test_an_early_close_ends_with_its_last_bar():
    rows = [(utc(EARLY, 14, 30), 10.0, 10.0, 10.0, 10.0, 1.0), (utc(EARLY, 17, 29), 11.0, 11.0, 11.0, 11.0, 1.0),
            (utc(EARLY, 17, 59), 12.0, 12.0, 12.0, 12.0, 1.0)]
    s = sessions([EARLY], rows, [EARLY])
    assert s.end_time[0] == utc(EARLY, 18, 0)
    assert_allclose(features.last_n_min_return(s, 30), [12 / 11 - 1])


def test_reclaiming_the_previous_low_uses_adjusted_prices():
    # A 2:1 split on Wednesday: Tuesday's raw low 200 is 100 adjusted; Wednesday dips to 98 and closes at 101.
    tuesday = [(utc(TUE, 13, 30), 210.0, 220.0, 200.0, 205.0, 1.0)]
    wednesday = [(utc(WED, 13, 30), 99.0, 102.0, 98.0, 101.0, 1.0)]
    s = sessions([MON, TUE, WED], tuesday + wednesday, [TUE, WED], multiplier=[0.5, 0.5, 1.0])
    assert_allclose(features.reclaim_prev_low(s), [NAN, NAN, 1.0], equal_nan=True)
    raw = sessions([MON, TUE, WED], tuesday + wednesday, [TUE, WED])
    assert_allclose(features.reclaim_prev_low(raw), [NAN, NAN, 0.0], equal_nan=True)
    after_a_gap = sessions([MON, TUE, WED], MONDAY + wednesday, [MON, WED])
    assert_allclose(features.reclaim_prev_low(after_a_gap), [NAN, NAN, NAN], equal_nan=True)


def minute_inputs() -> Inputs:
    """Two sessions of three bars; the second symbol has no bar at the first time of each session."""
    open_ = np.array([[10, 20], [11, 21], [12, 22], [30, 40], [31, 41], [32, 42]], dtype=float)
    high, low, close = open_ + 1, open_ - 1, open_ + 0.5
    volume = np.full(open_.shape, 100.0)
    real = np.array([[True, False], [True, True], [True, True], [True, False], [True, True], [True, True]])
    return Inputs(open_, high, low, close, volume, 252 * 390, np.array([0, 0, 0, 1, 1, 1]), real,
                  np.array([1.0, 2.0, 3.0, 1.0, 2.0, 3.0]))


def test_today_so_far_values_count_only_the_sessions_real_bars():
    inputs = minute_inputs()
    assert_allclose(features.today_open(inputs), [[10, NAN], [10, 21], [10, 21], [30, NAN], [30, 41], [30, 41]], equal_nan=True)
    assert_allclose(features.today_high(inputs), [[11, NAN], [12, 22], [13, 23], [31, NAN], [32, 42], [33, 43]], equal_nan=True)
    assert_allclose(features.today_low(inputs), [[9, NAN], [9, 20], [9, 20], [29, NAN], [29, 40], [29, 40]], equal_nan=True)
    assert_allclose(features.today_return(inputs)[:, 0], [0.5 / 10, 1.5 / 10, 2.5 / 10, 0.5 / 30, 1.5 / 30, 2.5 / 30])
    typical = (inputs.high + inputs.low + inputs.close) / 3
    assert_allclose(features.today_vwap(inputs)[:3, 1], [NAN, typical[1, 1], (typical[1, 1] + typical[2, 1]) / 2], equal_nan=True)
    assert_allclose(features.minutes_since_open(inputs), np.repeat([[1.0], [2.0], [3.0]] * 2, 2, axis=1))


def test_daily_values_on_a_minute_timeline_come_from_the_previous_session():
    daily_days = np.array([MON, TUE, WED])
    values = np.array([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]])
    session_days = np.array([MON, MON, TUE, WED, WED, WED + 1])
    assert_allclose(
        features.previous_session(daily_days, values, session_days),
        [[NAN, NAN], [NAN, NAN], [1, 10], [2, 20], [2, 20], [3, 30]], equal_nan=True,
    )
