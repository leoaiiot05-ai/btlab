"""Exit overlays and intrabar paths (SPEC.md §10.7, §10.8, AC-10)."""

import math

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.engine import simulate
from btlab.exits import (
    ExitRules,
    Overlays,
    Path,
    Watch,
    daily_path,
    ibguard_step,
    minute_path,
    walk,
)
from btlab.markets import US
from btlab.sizing import Sizing
from builders import DAY0, Script, bars, direction, settings, tw_costs, us_costs, weights

NAN = math.nan


# ----- paths -----


def test_the_daily_path_visits_the_low_first_on_an_up_bar():
    up, down = daily_path(10.0, 12.0, 9.0, 11.0), daily_path(10.0, 12.0, 9.0, 9.5)
    assert up.points.tolist() == [10, 9, 12, 11] and down.points.tolist() == [10, 12, 9, 9.5]
    assert up.gaps.tolist() == [True, False, False, False]


def test_the_minute_path_marks_a_gap_between_minutes():
    path = minute_path(np.array([10.0, 10.4, 10.1]), np.array([10.5, 10.6, 10.3]), np.array([9.8, 10.1, 10.0]),
                       np.array([10.2, 10.1, 10.2]))
    assert path.points.tolist() == [10, 9.8, 10.5, 10.2, 10.4, 10.6, 10.1, 10.1, 10.1, 10.0, 10.3, 10.2]
    assert path.gaps.tolist() == [True, False, False, False, True, False, False, False, False, False, False, False]


# ----- the ibguard rule (§10.8 vectors) -----

VECTORS = [
    ("L1", 1, [100, 104, 103], [100, 100, 104], None),
    ("L2", 1, [100, 104, 103, 98.7], [100, 100, 104, 104], ("STOP_LOSS", 98.7)),
    ("L3", 1, [100, 116], [100, 100], ("TAKE_PROFIT", 116)),
    ("L4", 1, [100, 110, 104], [100, 100, 110], ("STOP_LOSS", 104)),
    ("L5", 1, [120], [100], ("TAKE_PROFIT", 120)),
    ("L6", 1, [94], [100], ("STOP_LOSS", 94)),
    ("L7", 1, [100, 100], [100, 100], None),
    ("L8", 1, [97, 98, 97.5], [100, 100, 100], None),
    ("L9", 1, [100, 114.9, 114.8, 131], [100, 100, 114.9, 114.9], None),
    ("L10", 1, [100, 104, -1, NAN, 0, 103], [100, 100, 100, 100, 100, 104], None),
    ("S1", -1, [100, 96, 97], [100, 100, 96], None),
    ("S2", -1, [100, 96, 97, 100.9], [100, 100, 96, 96], ("STOP_LOSS", 100.9)),
    ("S3", -1, [100, 84], [100, 100], ("TAKE_PROFIT", 84)),
    ("S4", -1, [100, 90, 95], [100, 100, 90], ("STOP_LOSS", 95)),
    ("S5", -1, [106], [100], ("STOP_LOSS", 106)),
]


@pytest.mark.parametrize("name, side, points, refs, expected", VECTORS, ids=[v[0] for v in VECTORS])
def test_the_ibguard_vectors(name, side, points, refs, expected):
    ref, prev, seen, outcome = 100.0, None, [], None
    for point in points:
        ref, prev, result = ibguard_step(side, ref, prev, point, 0.15, 0.05)
        seen.append(ref)
        if result is not None:
            outcome = (result, point)
            break
    assert seen == refs[: len(seen)] and len(seen) == len(refs)
    assert outcome == expected


@pytest.mark.parametrize("name, side, points, refs, expected", VECTORS, ids=[v[0] for v in VECTORS])
def test_the_vectors_as_path_points_give_the_same_exits(name, side, points, refs, expected):
    """Every point a gap, as ticks are: the overlay exits where ibguard does, at the point's price."""
    path = Path(np.array(points, dtype=float), np.ones(len(points), dtype=bool))
    watch = Watch(100.0, 100.0, None)
    hit = walk(path, side, 100.0, None, ExitRules(ibguard_tp_pct=15, ibguard_sl_pct=5), watch)
    names = {"TAKE_PROFIT": "IBG_TP", "STOP_LOSS": "IBG_SL"}
    assert (None if hit is None else (hit.reason, hit.price)) == (None if expected is None else (names[expected[0]], expected[1]))


# ----- triggering along a path -----


def exit_of(path: Path, rules: ExitRules, side: int = 1, entry: float = 100.0, atr_distance=None, extreme=None):
    hit = walk(path, side, entry, atr_distance, rules, Watch(extreme or entry, entry, entry))
    return None if hit is None else (hit.reason, pytest.approx(hit.price))


@pytest.mark.parametrize(
    "path, rules, side, expected",
    [
        (daily_path(99, 100, 94, 96), ExitRules(stop_loss_pct=5), 1, ("SL", 95)),
        (daily_path(93, 97, 92, 96), ExitRules(stop_loss_pct=5), 1, ("SL", 93)),  # gapped below the stop
        (daily_path(101, 112, 100, 111), ExitRules(take_profit_pct=10), 1, ("TP", 110)),
        (daily_path(115, 116, 113, 114), ExitRules(take_profit_pct=10), 1, ("TP", 115)),
        (daily_path(99, 100, 95, 96), ExitRules(atr_n=14, atr_mult=2), 1, ("ATR", 96)),
        (Path(np.array([100.0, 110, 104]), np.array([True, False, False])), ExitRules(ibguard_tp_pct=15, ibguard_sl_pct=5), 1,
         ("IBG_SL", 104.5)),
        (daily_path(101, 106, 100, 104), ExitRules(stop_loss_pct=5), -1, ("SL", 105)),
        (daily_path(99, 100, 89, 95), ExitRules(take_profit_pct=10), -1, ("TP", 90)),
        (daily_path(99, 100, 98, 99.5), ExitRules(stop_loss_pct=5, take_profit_pct=10), 1, None),
    ],
    ids=["SL in a segment", "SL at a gap", "TP in a segment", "TP at a gap", "ATR", "ibguard ratchet", "short SL", "short TP",
         "no exit"],
)
def test_each_overlay(path, rules, side, expected):
    assert exit_of(path, rules, side, atr_distance=4.0 if rules.atr_n else None) == expected


def test_the_trailing_stop_follows_the_peak_across_bars():
    rules, watch = ExitRules(trailing_pct=5), Watch(100.0, 100.0, 100.0)
    assert walk(daily_path(100, 110, 99, 109), 1, 100.0, None, rules, watch) is None
    assert watch.extreme == 110
    hit = walk(daily_path(108, 108.5, 103, 104), 1, 100.0, None, rules, watch)
    assert (hit.reason, hit.price) == ("TRAIL", pytest.approx(104.5))


def test_the_short_trailing_stop_follows_the_trough():
    rules, watch = ExitRules(trailing_pct=5), Watch(100.0, 100.0, 100.0)
    assert walk(daily_path(100, 101, 90, 91), -1, 100.0, None, rules, watch) is None
    hit = walk(daily_path(92, 95, 91, 94), -1, 100.0, None, rules, watch)
    assert (hit.reason, hit.price, watch.extreme) == ("TRAIL", pytest.approx(94.5), 90)


def test_the_earliest_crossing_in_a_segment_wins():
    rules = ExitRules(stop_loss_pct=5, atr_n=14, atr_mult=1)
    assert exit_of(daily_path(100, 100, 90, 91), rules, atr_distance=4.0) == ("ATR", 96)
    assert exit_of(daily_path(100, 100, 90, 91), rules, atr_distance=6.0) == ("SL", 95)


def test_a_tie_goes_to_the_stop():
    tie = ExitRules(stop_loss_pct=5, ibguard_tp_pct=15, ibguard_sl_pct=5)
    assert exit_of(daily_path(100, 100, 90, 91), tie) == ("SL", 95)
    # A profit level below the stop, as an odd configuration could give: both are crossed at the gap.
    odd = ExitRules(stop_loss_pct=2, take_profit_pct=-2)
    assert exit_of(Path(np.array([98.0]), np.array([True])), odd) == ("SL", 98)


def test_a_minute_path_can_reverse_the_daily_order():
    rules = ExitRules(stop_loss_pct=5, take_profit_pct=10)
    # The day closes at its open, so the daily path takes the low first; the minutes went up first.
    assert exit_of(daily_path(100, 111, 94, 100), rules) == ("SL", 95)
    minutes = minute_path(np.array([100.0, 111.0]), np.array([111.0, 111.0]), np.array([100.0, 94.0]), np.array([111.0, 100.0]))
    assert exit_of(minutes, rules) == ("TP", 110)


# ----- through the engine -----


def run(timeline, strategy, rules, **options):
    options.setdefault("costs", us_costs(len(timeline.symbols), borrow=0.0))
    return simulate(timeline, strategy, settings(capital=10_000.0, **options), Overlays(rules))


def test_an_exit_fills_with_slippage_and_commission_and_ends_the_trade():
    timeline = bars([[100.0], [100.0], [96.0], [96.0]], opens=[[100], [100], [99], [96]], highs=[[100], [100], [100], [96]],
                    lows=[[100], [100], [94], [96]])
    costs = us_costs(commission=US.commission, slippage=5, borrow=0.0)
    result = run(timeline, direction([1], [1], [1], [1]), ExitRules(stop_loss_pct=5), costs=costs, fill_timing="SAME_CLOSE",
                 sizing=Sizing("FIXED_SHARES", shares=10))
    buy, sell = result.fills
    entry = 100 * 1.0005
    assert (sell.time, sell.reason, sell.price) == (DAY0 + 2, "SL", pytest.approx(entry * 0.95 * 0.9995))
    assert sell.costs == pytest.approx(1.0)
    (trade,) = result.trades
    assert trade.exit_reason == "SL"
    assert_array_equal(result.quantity[:, 0], [10, 10, 0, 0])  # no re-entry while the direction stays +1


def test_a_position_opened_at_the_open_meets_that_bars_path():
    timeline = bars([[100.0], [100.0], [90.0]], opens=[[100], [100], [100]], highs=[[100], [100], [100]], lows=[[100], [100], [88]])
    result = run(timeline, direction([0], [1], [1]), ExitRules(stop_loss_pct=5), sizing=Sizing("FIXED_SHARES", shares=10))
    assert [(f.time, f.side, f.reason, f.price) for f in result.fills] == [
        (DAY0 + 2, "BUY", "SIGNAL", 100.0), (DAY0 + 2, "SELL", "SL", pytest.approx(95.0))
    ]


@pytest.mark.parametrize(
    "timing, bars_held, exit_bar",
    [("NEXT_OPEN", 1, 3), ("NEXT_OPEN", 3, 5), ("SAME_CLOSE", 3, 3), ("SAME_CLOSE", 1, 2)],
)
def test_max_hold_under_both_fill_timings(timing, bars_held, exit_bar):
    # The signal is at bar 1: NEXT_OPEN enters at bar 2's open, SAME_CLOSE at bar 1's close.
    timeline = bars([[10.0]] * 8)
    result = run(timeline, direction(*[[0]] + [[1]] * 7), ExitRules(max_hold_bars=bars_held), fill_timing=timing,
                 sizing=Sizing("FIXED_SHARES", shares=10))
    sell = [f for f in result.fills if f.side == "SELL"][0]
    assert (sell.reason, sell.time) == ("MAX_HOLD", DAY0 + exit_bar)
    assert len(result.fills) == 2  # the direction did not change, so no re-entry


def test_max_hold_replaces_the_strategys_order_at_the_same_close():
    timeline = bars([[10.0]] * 5)
    result = run(timeline, weights([0.5], [0.5], [0.5], [0.5], [0.5], rebalance=[True] * 5), ExitRules(max_hold_bars=2),
                 fill_timing="SAME_CLOSE")
    # Bought at bar 0; at bar 1 the exit is due and wins over the rebalance; the rebalance of bar 2 buys again.
    assert_array_equal(result.quantity[:, 0], [500, 0, 500, 0, 500])
    assert [f.reason for f in result.fills] == ["SIGNAL", "MAX_HOLD", "SIGNAL", "MAX_HOLD", "SIGNAL"]


def test_re_entry_waits_for_the_strategy_target_to_change():
    closes = [[100.0], [100.0], [94.0], [97.0], [97.0], [97.0], [98.0]]
    timeline = bars(closes, lows=[[100], [100], [93], [97], [97], [97], [98]])
    result = run(timeline, direction([1], [1], [1], [1], [0], [1], [1]), ExitRules(stop_loss_pct=5), fill_timing="SAME_CLOSE",
                 sizing=Sizing("FIXED_SHARES", shares=10))
    assert_array_equal(result.quantity[:, 0], [10, 10, 0, 0, 0, 10, 10])


def test_weights_re_enter_at_the_next_rebalance_bar():
    closes = [[100.0], [100.0], [94.0], [97.0], [97.0]]
    timeline = bars(closes, lows=[[100], [100], [93], [97], [97]])
    targets = weights([0.5], [0.5], [0.5], [0.5], [0.5], rebalance=[True, False, False, False, True])
    result = run(timeline, targets, ExitRules(stop_loss_pct=5), fill_timing="SAME_CLOSE")
    assert_array_equal(result.quantity[:, 0], [50, 50, 0, 0, 50])


def test_levels_are_divided_by_the_share_ratio_and_kept_through_dividends():
    split = bars([[100.0], [100.0], [48.0], [47.0]], opens=[[100], [100], [48], [48]], ratio=[[1], [1], [2], [1]])
    result = run(split, direction([1], [1], [1], [1]), ExitRules(stop_loss_pct=5), fill_timing="SAME_CLOSE",
                 sizing=Sizing("FIXED_SHARES", shares=10))
    sell = result.fills[-1]
    assert (sell.reason, sell.time, sell.price, sell.quantity) == ("SL", DAY0 + 3, pytest.approx(47.5), 20)
    dividend = bars([[100.0], [100.0], [94.5]], lows=[[100], [100], [94.5]], cash=[[0], [0], [1.0]])
    result = run(dividend, direction([1], [1], [1]), ExitRules(stop_loss_pct=5), fill_timing="SAME_CLOSE",
                 sizing=Sizing("FIXED_SHARES", shares=10))
    assert result.fills[-1].reason == "SL"  # the ex-dividend drop counts as a price move


def test_an_atr_stop_uses_the_raw_scale_at_the_signal_bar():
    # Adjusted bars range 1 a day and raw ones 2: ATR(2) at the signal bar is 1 adjusted, 2 raw; the stop is 2 × 2 below.
    timeline = bars([[100.0]] * 4 + [[95.0]], opens=[[100.0]] * 5, highs=[[101.0]] * 4 + [[100.0]], lows=[[99.0]] * 4 + [[95.0]],
                    adjust=[[0.5]] * 5)
    result = run(timeline, direction([0], [0], [1], [1], [1]), ExitRules(atr_n=2, atr_mult=2), fill_timing="SAME_CLOSE",
                 sizing=Sizing("FIXED_SHARES", shares=10))
    sell = result.fills[-1]
    assert (sell.reason, sell.price) == ("ATR", pytest.approx(96.0))


def test_a_blocked_exit_waits_with_its_reason():
    # A limit-down day opens below the stop: the sell cannot fill, so it fills at the next open.
    timeline = bars([[100.0], [100.0], [90.0], [91.0]], opens=[[100], [100], [90], [92]], lock=[[0], [0], [-1], [0]])
    result = simulate(timeline, direction([1], [1], [1], [1]),
                      settings(tw_costs(taxes=None), "TW", 1_000_000.0, sizing=Sizing("FIXED_SHARES", shares=1_000)),
                      Overlays(ExitRules(stop_loss_pct=5)))
    sell = result.fills[-1]
    assert (sell.reason, sell.time, sell.price) == ("SL", DAY0 + 3, 92.0)


def test_exit_fills_pay_the_tw_tax():
    timeline = bars([[100.0], [100.0], [94.0]], opens=[[100], [100], [100]])
    result = simulate(timeline, direction([1], [1], [1]),
                      settings(tw_costs(), "TW", 1_000_000.0, sizing=Sizing("FIXED_SHARES", shares=1_000), fill_timing="SAME_CLOSE"),
                      Overlays(ExitRules(stop_loss_pct=5)))
    sell = result.fills[-1]
    assert sell.reason == "SL" and sell.costs == pytest.approx(1_000 * 95 * 0.003)


class Minutes:
    """Minute bars for some days of symbol 0, as a daily timeline's MinutePaths."""

    def __init__(self, days: dict[int, tuple]) -> None:
        self.days = days

    def bars(self, t: int, j: int):
        found = self.days.get(t)
        return None if found is None else tuple(np.array(column, dtype=float) for column in found)


def test_the_minute_path_and_path_fallback():
    closes = [[100.0], [100.0], [101.0], [100.0]]
    timeline = bars(closes, highs=[[100], [100], [102], [111]], lows=[[100], [100], [99], [94]])
    minutes = Minutes({3: ([100, 111], [111, 111], [100, 94], [111, 100])})
    rules = ExitRules(stop_loss_pct=5, take_profit_pct=10)
    result = simulate(timeline, direction([1], [1], [1], [1]), settings(us_costs(borrow=0.0), fill_timing="SAME_CLOSE",
                                                                       sizing=Sizing("FIXED_SHARES", shares=10)),
                      Overlays(rules, minutes))
    assert (result.fills[-1].reason, result.fills[-1].price) == ("TP", pytest.approx(110.0))
    assert result.flags == {"PATH_FALLBACK": 2}  # bars 1 and 2 had no minute bars
    daily = simulate(timeline, direction([1], [1], [1], [1]), settings(us_costs(borrow=0.0), fill_timing="SAME_CLOSE",
                                                                      sizing=Sizing("FIXED_SHARES", shares=10)),
                     Overlays(rules))
    assert daily.fills[-1].reason == "SL" and daily.flags == {}
