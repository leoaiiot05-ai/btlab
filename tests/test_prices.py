"""Corporate actions, adjusted series and alignment (SPEC.md §6, AC-4, AC-5)."""

import math
from datetime import date

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from btlab.prices import (
    ActionIssue,
    adjusted,
    align,
    day_events,
    normalize_tw_actions,
    normalize_us_actions,
)
from btlab.records import (
    Action,
    CapitalReduction,
    DailyBars,
    DividendPolicy,
    DividendResult,
    ParValueChange,
    SplitPrice,
    to_day,
)

D1, D2, D3 = 20000, 20001, 20002


def bars(days, closes, volume=None, no_trade=None, opens=None) -> DailyBars:
    close = np.array(closes, dtype=np.float64)
    open_ = close if opens is None else np.array(opens, dtype=np.float64)
    return DailyBars(
        np.array(days, dtype=np.int64), open_.copy(), np.fmax(open_, close) + 1, np.fmin(open_, close) - 1, close,
        np.full(len(close), 1000.0) if volume is None else np.array(volume, dtype=np.float64),
        np.zeros(len(close), dtype=bool) if no_trade is None else np.array(no_trade, dtype=bool),
    )


def adjusted_closes(market, stored: DailyBars, actions) -> np.ndarray:
    events, issues = day_events(market, stored, actions)
    assert issues == []
    return adjusted(stored, events).close


def tw(results=(), policies=(), splits=(), par=(), reductions=()):
    return normalize_tw_actions(list(results), list(policies), list(splits), list(par), list(reductions))


# ----- the examples of §6.3 -----


def test_a1_a_us_cash_dividend():
    actions, issues = normalize_us_actions([Action(D2, "CASH_DIV", "DIV_RESULT", cash=2.0)])
    assert issues == []
    assert_allclose(adjusted_closes("US", bars([D1, D2], [100, 99]), actions), [98, 99])


def test_a2_a_us_split_doubles_earlier_volume():
    stored = bars([D1, D2], [100, 51], volume=[1000, 2500])
    actions, _ = normalize_us_actions([Action(D2, "SHARE_RATIO", "SPLIT", ratio=2.0)])
    events, _ = day_events("US", stored, actions)
    result = adjusted(stored, events)
    assert_allclose(result.close, [50, 51])
    assert_allclose(result.volume, [2000, 2500])


def test_a3_a_us_split_and_dividend_on_one_day():
    actions, _ = normalize_us_actions(
        [Action(D2, "SHARE_RATIO", "SPLIT", ratio=2.0), Action(D2, "CASH_DIV", "DIV_RESULT", cash=0.5)]
    )
    assert_allclose(adjusted_closes("US", bars([D1, D2], [100, 49]), actions), [49.5, 49])


def test_a4_a_tw_cash_dividend():
    day = to_day(date(2019, 6, 24))
    actions, issues = tw(results=[DividendResult(day, 248.5, 240.5, "息")])
    assert issues == []
    assert actions == [Action(day, "CASH_DIV", "DIV_RESULT", cash=8.0, ref_before=248.5, ref_after=240.5)]
    assert_allclose(adjusted_closes("TW", bars([day - 3, day], [248.5, 240.5]), actions), [240.5, 240.5])


def test_a5_a_tw_stock_and_cash_dividend():
    actions, issues = tw(results=[DividendResult(D2, 50, 43.6364, "權息")], policies=[DividendPolicy(D2, 2.0)])
    assert issues == []
    cash, shares = actions
    assert (cash.kind, cash.cash, cash.detail) == ("CASH_DIV", 2.0, "DIV_RESULT")
    assert (shares.kind, shares.detail) == ("SHARE_RATIO", "STOCK_DIV")
    assert shares.ratio == pytest.approx(1.1, rel=1e-5)
    assert_allclose(adjusted_closes("TW", bars([D1, D2], [50, 42]), actions), [43.6364, 42])


def test_a6_a_tw_reverse_split():
    day = to_day(date(2024, 12, 11))
    actions, issues = tw(splits=[SplitPrice(day, "00632R", "反分割", 3.28, 22.96)])
    assert issues == []
    assert actions == [Action(day, "SHARE_RATIO", "REVERSE_SPLIT", ratio=3.28 / 22.96)]
    assert_allclose(adjusted_closes("TW", bars([day - 1, day], [3.28, 22.96]), actions), [22.96, 22.96])


# ----- normalization (§6.2) -----


@pytest.mark.parametrize(
    "kind, policies, expected, issue",
    [
        ("息", [DividendPolicy(D2, 3.5), DividendPolicy(D2, 0.5), DividendPolicy(D3, 9.0)], [("CASH_DIV", 4.0, 1.0)], None),
        ("息", [DividendPolicy(D3, 9.0), DividendPolicy(None, 1.0)], [("CASH_DIV", 10.0, 1.0)], None),
        ("權", [], [("SHARE_RATIO", 0.0, 1.25)], None),
        ("權息", [DividendPolicy(D2, 2.0)], [("CASH_DIV", 2.0, 1.0), ("SHARE_RATIO", 0.0, 1.2)], None),
        ("權息", [], [("SHARE_RATIO", 0.0, 1.25)], "DIVIDEND_SPLIT_UNKNOWN"),
    ],
)
def test_each_tw_dividend_result_case(kind, policies, expected, issue):
    actions, issues = tw(results=[DividendResult(D2, 50.0, 40.0, kind)], policies=policies)
    assert [(a.kind, a.cash, pytest.approx(a.ratio)) for a in actions] == expected
    assert all((a.ref_before, a.ref_after) == (50.0, 40.0) for a in actions)
    assert all(a.detail == ("DIV_RESULT" if a.kind == "CASH_DIV" else "STOCK_DIV") for a in actions)
    assert [i.kind for i in issues] == ([issue] if issue else [])


def test_a_ratio_within_a_millionth_of_1_is_dropped():
    actions, issues = tw(results=[DividendResult(D2, 50.00001, 50.0, "權")])
    assert actions == [] and issues == []
    actions, issues = normalize_us_actions([Action(D2, "SHARE_RATIO", "SPLIT", ratio=1 + 5e-7)])
    assert actions == [] and issues == []


def test_tw_share_events_and_their_ratios():
    actions, issues = tw(
        splits=[SplitPrice(D1, "2330", "分割", 100.0, 25.0), SplitPrice(D1, "2330", "分割", 100.0, 25.0)],
        par=[ParValueChange(D2, "2330", 900.0, 90.0)],
        reductions=[CapitalReduction(D3, 30.0, 40.0, "彌補虧損")],
    )
    assert issues == []
    assert actions == [
        Action(D1, "SHARE_RATIO", "SPLIT", ratio=4.0),
        Action(D2, "SHARE_RATIO", "PAR_CHANGE", ratio=10.0),
        Action(D3, "SHARE_RATIO", "CAPITAL_REDUCTION", ratio=0.75),
    ]


def test_events_of_one_day_merge_and_their_factors_multiply():
    actions, issues = tw(
        results=[DividendResult(D2, 50.0, 40.0, "權")], par=[ParValueChange(D2, "2330", 900.0, 90.0)]
    )
    assert issues == []
    assert actions == [Action(D2, "SHARE_RATIO", "STOCK_DIV", ratio=12.5, ref_before=50.0, ref_after=4.0)]
    stored = bars([D1, D2], [50.0, 4.0], volume=[1000, 1000])
    events, _ = day_events("TW", stored, actions)
    assert_allclose(events.factor, [40 / 50 / 10])
    result = adjusted(stored, events)
    assert_allclose(result.close, [4.0, 4.0])
    assert_allclose(result.volume, [12500, 1000])


@pytest.mark.parametrize(
    "action",
    [
        Action(D2, "CASH_DIV", "DIV_RESULT", cash=math.nan),
        Action(D2, "CASH_DIV", "DIV_RESULT", cash=math.inf),
        Action(D2, "CASH_DIV", "DIV_RESULT", cash=0.0),
        Action(D2, "CASH_DIV", "DIV_RESULT", cash=-1.0),
        Action(D2, "SHARE_RATIO", "SPLIT", ratio=0.0),
        Action(D2, "SHARE_RATIO", "REVERSE_SPLIT", ratio=-0.5),
        Action(D2, "SHARE_RATIO", "SPLIT", ratio=math.nan),
        Action(D2, "BONUS", "SPLIT", ratio=2.0),
    ],
)
def test_bad_us_actions_are_skipped_with_bad_action(action):
    good = Action(D3, "CASH_DIV", "DIV_RESULT", cash=1.0)
    actions, issues = normalize_us_actions([action, good])
    assert actions == [good]
    assert [(i.kind, i.day) for i in issues] == [("BAD_ACTION", D2)]


@pytest.mark.parametrize(
    "rows",
    [
        {"results": [DividendResult(D2, 0.0, 40.0, "息")]},
        {"results": [DividendResult(D2, 50.0, math.nan, "權")]},
        {"results": [DividendResult(D2, 50.0, 40.0, "其他")]},
        {"results": [DividendResult(D2, 40.0, 50.0, "息")]},  # a negative dividend
        {"splits": [SplitPrice(D2, "2330", "分割", 100.0, 0.0)]},
        {"par": [ParValueChange(D2, "2330", -10.0, 1.0)]},
        {"reductions": [CapitalReduction(D2, 30.0, math.inf, None)]},
    ],
)
def test_bad_tw_rows_are_skipped_with_bad_action(rows):
    actions, issues = tw(**rows)
    assert actions == []
    assert [(i.kind, i.day) for i in issues] == [("BAD_ACTION", D2)]
    assert isinstance(issues[0], ActionIssue) and issues[0].message


def test_a_second_dividend_result_on_one_day_is_skipped():
    first, second = DividendResult(D2, 50.0, 48.0, "息"), DividendResult(D2, 50.0, 45.0, "息")
    actions, issues = tw(results=[first, first, second])
    assert [a.cash for a in actions] == [2.0]
    assert [i.kind for i in issues] == ["BAD_ACTION"]


# ----- factors (§6.3) -----


def test_a_factor_out_of_range_ignores_its_dividend_part():
    stored = bars([D1, D2, D3], [10.0, 5.0, 5.0])
    actions = [Action(D2, "CASH_DIV", "DIV_RESULT", cash=12.0), Action(D2, "SHARE_RATIO", "SPLIT", ratio=2.0)]
    events, issues = day_events("US", stored, actions)
    assert [(i.kind, i.day) for i in issues] == [("BAD_ACTION", D2)]
    assert_allclose(events.factor, [0.5])
    assert_allclose(events.cash, [12.0])


def test_a_tw_reference_beyond_twice_the_price_is_bad():
    actions = [Action(D2, "CASH_DIV", "DIV_RESULT", cash=1.0, ref_before=10.0, ref_after=25.0)]
    events, issues = day_events("TW", bars([D1, D2], [10.0, 25.0]), actions)
    assert [i.kind for i in issues] == ["BAD_ACTION"]
    assert_allclose(events.factor, [1.0])


def test_adjusted_volumes_and_prices_across_several_events():
    stored = bars([D1, D2, D3, D3 + 1], [120.0, 60.0, 20.0, 20.0], volume=[10.0, 20.0, 30.0, 40.0])
    actions = [Action(D2, "SHARE_RATIO", "SPLIT", ratio=2.0), Action(D3, "SHARE_RATIO", "SPLIT", ratio=3.0)]
    events, _ = day_events("US", stored, actions)
    result = adjusted(stored, events)
    assert_allclose(result.close, [20.0, 20.0, 20.0, 20.0])
    assert_allclose(result.volume, [60.0, 60.0, 30.0, 40.0])
    assert_array_equal(result.no_trade, stored.no_trade)


def test_events_after_the_last_bar_still_adjust_and_a_first_day_event_has_no_dividend_part():
    stored = bars([D1, D2], [100.0, 100.0])
    events, issues = day_events("US", stored, [Action(D3, "CASH_DIV", "DIV_RESULT", cash=1.0)])
    assert issues == []
    assert_allclose(adjusted(stored, events).close, [99.0, 99.0])
    events, issues = day_events("US", stored, [Action(D1, "CASH_DIV", "DIV_RESULT", cash=1.0), Action(D1, "SHARE_RATIO", "SPLIT", ratio=4.0)])
    assert issues == []
    assert_allclose(events.factor, [0.25])


# ----- alignment (§6.4, §6.5) -----


def test_alignment_carries_closes_over_gaps_and_no_trade_days():
    calendar = np.arange(D1, D1 + 7, dtype=np.int64)
    a = bars([D1, D1 + 1, D1 + 3, D1 + 4, D1 + 5], [10.0, 11.0, math.nan, 12.0, 13.0],
             volume=[100, 200, 300, 0, 500], no_trade=[False, False, True, False, False], opens=[10, 10.5, math.nan, 12, 12.5])
    b = bars([D1 + 2, D1 + 3], [50.0, 51.0])
    aligned, issues = align("US", calendar, {"A": (a, []), "B": (b, [])})
    assert issues == [] and aligned.symbols == ("A", "B")
    assert_array_equal(aligned.days, calendar)
    nan = math.nan
    assert_array_equal(aligned.close[:, 0], [10, 11, 11, 11, 12, 13, nan])
    assert_array_equal(aligned.open[:, 0], [10, 10.5, 11, 11, 12, 12.5, nan])
    assert_array_equal(aligned.high[:, 0], [11, 12, 11, 11, 13, 14, nan])
    assert_array_equal(aligned.volume[:, 0], [100, 200, 0, 0, 0, 500, nan])
    assert_array_equal(aligned.tradable[:, 0], [True, True, False, False, False, True, False])
    assert_array_equal(aligned.close[:, 1], [nan, nan, 50, 51, nan, nan, nan])
    assert_array_equal(aligned.tradable[:, 1], [False, False, True, True, False, False, False])
    assert_array_equal(aligned.volume[:, 1], [nan, nan, 1000, 1000, nan, nan, nan])
    assert aligned.close.shape == (7, 2) and aligned.tradable.dtype == bool and aligned.lock.dtype == np.int8


def test_alignment_adjusts_and_places_each_action_on_its_day():
    calendar = np.array([D1, D1 + 1, D1 + 4, D1 + 5], dtype=np.int64)  # a weekend between
    stored = bars([D1 - 5, D1, D1 + 1, D1 + 4, D1 + 5], [99.0, 100.0, 100.0, 49.0, 50.0], volume=[1, 10, 10, 30, 30])
    actions = [
        Action(D1 - 3, "CASH_DIV", "DIV_RESULT", cash=1.0),  # before the range: only in the multipliers
        Action(D1 + 2, "SHARE_RATIO", "SPLIT", ratio=2.0),  # a weekend day: applies on the next calendar day
        Action(D1 + 2, "CASH_DIV", "DIV_RESULT", cash=1.0),
    ]
    aligned, _ = align("US", calendar, {"A": (stored, actions)})
    assert_allclose(aligned.ratio[:, 0], [1, 1, 2, 1])
    assert_allclose(aligned.cash[:, 0], [0, 0, 1, 0])
    factor = (100 / 2 - 1) / 100
    assert_allclose(aligned.multiplier[:, 0], [factor, factor, 1, 1])
    assert_allclose(aligned.adj_close[:, 0], [100 * factor, 100 * factor, 49, 50])
    assert_allclose(aligned.adj_volume[:, 0], [20, 20, 30, 30])
    assert_allclose(aligned.close[:, 0], [100, 100, 49, 50])


def test_carried_adjusted_prices_stay_flat_across_an_ex_date():
    calendar = np.array([D1, D2, D3], dtype=np.int64)
    stored = bars([D1, D3], [100.0, 50.0])
    aligned, _ = align("US", calendar, {"A": (stored, [Action(D2, "SHARE_RATIO", "SPLIT", ratio=2.0)])})
    assert_allclose(aligned.close[:, 0], [100, 100, 50])
    assert_allclose(aligned.adj_close[:, 0], [50, 50, 50])
    assert_allclose(aligned.ratio[:, 0], [1, 2, 1])


def test_the_lock_uses_the_previous_close_times_the_days_factor():
    day = to_day(date(2026, 10, 28))
    calendar = np.array([day, day + 1, day + 2], dtype=np.int64)
    # Ex-dividend on the second day: ref = 100 × 90/100 = 90, so a flat bar at 90 is no move; the third day is limit up.
    stored = DailyBars(calendar, np.array([100.0, 90.0, 99.0]), np.array([101.0, 90.0, 99.0]), np.array([99.0, 90.0, 99.0]),
                       np.array([100.0, 90.0, 99.0]), np.array([10.0, 10.0, 10.0]), np.zeros(3, dtype=bool))
    actions, _ = tw(results=[DividendResult(day + 1, 100.0, 90.0, "息")])
    aligned, _ = align("TW", calendar, {"2330": (stored, actions), "2317": (stored, [])})
    assert_array_equal(aligned.lock[:, 0], [0, 0, 1])
    assert_array_equal(aligned.lock[:, 1], [0, -1, 1])


def test_no_symbols_or_no_days_align_to_empty_arrays():
    aligned, _ = align("US", np.arange(D1, D3), {})
    assert aligned.close.shape == (2, 0)
    aligned, _ = align("US", np.array([], dtype=np.int64), {"A": (bars([D1], [1.0]), [])})
    assert aligned.close.shape == (0, 1)
