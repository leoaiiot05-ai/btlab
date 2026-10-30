"""The engine core on hand-made bars (SPEC.md §9.1, §9.4, §10.3 to §10.7, AC-5, AC-7, AC-8, AC-9)."""

from datetime import date

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from btlab.engine import Timeline, simulate
from btlab.markets import TW, US
from btlab.records import ShortSuspension, to_day
from btlab.sizing import Sizing
from builders import DAY0, Script, bars, direction, settings, tw_costs, us_costs, weights


def assert_identity(timeline: Timeline, result, adjusted: bool = False) -> None:
    close = (timeline.adj_close if adjusted else timeline.close)[timeline.start:]
    values = result.quantity * close
    assert_allclose(result.equity, result.cash + np.nansum(np.where(values > 0, values, 0), axis=1)
                    - np.nansum(np.where(values < 0, -values, 0), axis=1), rtol=1e-12)
    assert_allclose(result.long_value - result.short_value, np.nansum(values, axis=1), rtol=1e-12, atol=1e-9)


# ----- AC-7: accounting, fills and costs -----


def test_equity_is_cash_plus_long_minus_short_on_every_bar():
    rng = np.random.default_rng(6)
    closes = 50 * np.exp(np.cumsum(rng.normal(0, 0.03, size=(40, 3)), axis=0))
    timeline = bars(closes, closes * np.exp(rng.normal(0, 0.01, size=closes.shape)), start=2)
    targets = weights(*np.round(rng.uniform(-0.6, 0.6, size=(40, 3)), 1))
    result = simulate(timeline, targets, settings(us_costs(3, US.commission, slippage=5), allow_short=True))
    assert len(result.fills) > 20
    assert_identity(timeline, result)
    assert_allclose(result.gross, (result.long_value + result.short_value) / result.equity)
    assert_allclose(result.net, (result.long_value - result.short_value) / result.equity)
    assert_array_equal(result.positions, (result.quantity != 0).sum(axis=1))


def test_next_open_fills_at_the_next_open_with_slippage():
    timeline = bars([[10.0], [10.0], [12.0], [12.0], [13.0]], [[10.0], [11.0], [11.5], [12.5], [13.0]])
    result = simulate(timeline, direction([0], [1], [1], [0], [0]), settings(us_costs(slippage=5), capital=1_000.0,
                                                                            sizing=Sizing(fraction=0.5)))
    buy, sell = result.fills
    assert (buy.time, buy.side, buy.quantity, buy.price) == (DAY0 + 2, "BUY", 50, pytest.approx(11.5 * 1.0005))
    assert (sell.time, sell.side, sell.quantity, sell.price) == (DAY0 + 4, "SELL", 50, pytest.approx(13 * 0.9995))
    assert result.cash[-1] == pytest.approx(1_000 - 50 * 11.5 * 1.0005 + 50 * 13 * 0.9995)
    assert_array_equal(result.quantity[:, 0], [0, 0, 50, 50, 0])


def test_same_close_fills_at_the_signal_bars_close():
    timeline = bars([[10.0], [10.0], [12.0], [12.0], [13.0]], [[10.0], [11.0], [11.5], [12.5], [13.0]])
    result = simulate(timeline, direction([0], [1], [1], [0], [0]), settings(
        us_costs(slippage=5), capital=1_000.0, sizing=Sizing(fraction=0.5), fill_timing="SAME_CLOSE"))
    buy, sell = result.fills
    assert (buy.time, buy.price, sell.time, sell.price) == (DAY0 + 1, pytest.approx(10.005), DAY0 + 3, pytest.approx(11.994))
    assert_array_equal(result.quantity[:, 0], [0, 50, 50, 0, 0])
    assert result.equity[1] == pytest.approx(1_000 - 50 * 10.005 + 50 * 10)


def test_reducing_orders_fill_before_increasing_ones():
    timeline = bars([[10.0, 20.0]] * 4)
    targets = weights([1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0])
    result = simulate(timeline, targets, settings(us_costs(2), capital=1_000.0, fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[-1], [0, 50])
    assert [(f.symbol, f.side) for f in result.fills] == [("A", "BUY"), ("A", "SELL"), ("B", "BUY")]


def test_increasing_orders_scale_by_one_factor_when_buying_power_is_short():
    timeline = bars([[10.0, 20.0]] * 2)
    result = simulate(timeline, weights([0.8, 0.8], [0.8, 0.8]), settings(us_costs(2), capital=1_000.0, fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[0], [50, 25])  # 80 and 40 scaled by 0.625
    assert result.cash[0] == pytest.approx(0.0)


def test_board_lots_and_sub_lot_orders():
    timeline = bars([[100.0]] * 6, ratio=[[1], [1], [1], [1.1], [1], [1]])
    targets = weights([0.15], [0.16], [0.25], [np.nan], [0.0], [np.nan], rebalance=[True] * 6)
    result = simulate(timeline, targets, settings(tw_costs(taxes=None), "TW", 1_000_000.0, board_lots_only=True,
                                                 fill_timing="SAME_CLOSE"))
    # 1,500 shares round to 1,000; 1,600 makes no order; 2,500 buys a lot; a stock dividend leaves 2,200; 0 sells them all.
    assert_array_equal(result.quantity[:, 0], [1_000, 1_000, 2_000, 2_200, 0, 0])
    assert [f.quantity for f in result.fills] == [1_000, 1_000, 2_200]


def test_commissions_per_fill_through_the_engine():
    timeline = bars([[50.0], [50.0], [50.0]])
    script = Script({0: [("order_target_qty", 0, 1)], 1: [("order_target_qty", 0, 101)], 2: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(us_costs(commission=US.commission), fill_timing="SAME_CLOSE"))
    assert [f.costs for f in result.fills] == [pytest.approx(0.5), pytest.approx(1.0), pytest.approx(1.0)]
    timeline = bars([[100.0], [100.0]])
    script = Script({0: [("order_target_qty", 0, 500)], 1: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(tw_costs(commission=TW.commission, taxes=None), "TW", 1_000_000.0,
                                                 fill_timing="SAME_CLOSE"))
    assert [f.costs for f in result.fills] == [pytest.approx(80.0), pytest.approx(80.0)]


@pytest.mark.parametrize(
    "category, iso, rate",
    [("STOCK", "2020-06-01", 0.3), ("ETF", "2020-06-01", 0.1), ("BOND_ETF", "2016-06-01", 0.1), ("BOND_ETF", "2018-06-01", 0.0),
     ("TDR", "2020-06-01", 0.1)],
)
def test_tw_sells_pay_the_tax_of_their_category_and_date(category, iso, rate):
    first = to_day(date.fromisoformat(iso))
    timeline = bars([[100.0], [100.0]], days=[first, first + 1])
    script = Script({0: [("order_target_qty", 0, 1_000)], 1: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(tw_costs([category]), "TW", 1_000_000.0, fill_timing="SAME_CLOSE"))
    buy, sell = result.fills
    assert buy.costs == 0.0
    assert sell.costs == pytest.approx(1_000 * 100 * rate / 100)


def day_trade_timeline() -> Timeline:
    """Two sessions of three bars each, on 2020-06-01 and 2020-06-02."""
    return bars([[100.0]] * 6, days=[DAY0] * 3 + [DAY0 + 1] * 3, sessions=[0, 0, 0, 1, 1, 1])


@pytest.mark.parametrize("sell_bar, rate", [(1, 0.15), (2, 0.3)], ids=["same session", "next session"])
def test_a_long_round_trip_in_one_session_pays_the_day_trade_rate(sell_bar, rate):
    script = Script({0: [("order_target_qty", 0, 1_000)], sell_bar: [("order_target_qty", 0, 0)]})
    result = simulate(day_trade_timeline(), script, settings(tw_costs(), "TW", 1_000_000.0))
    buy, sell = result.fills
    assert buy.time == DAY0 and sell.costs == pytest.approx(100_000 * rate / 100)


def test_a_short_covered_in_its_session_gets_back_the_day_trade_difference():
    script = Script({0: [("order_target_qty", 0, -1_000)], 1: [("order_target_qty", 0, 0)]})
    result = simulate(day_trade_timeline(), script, settings(tw_costs(), "TW", 1_000_000.0, allow_short=True))
    short, cover = result.fills
    assert short.costs == pytest.approx(100_000 * (0.3 + 0.08) / 100)
    assert cover.costs == pytest.approx(-100_000 * (0.3 - 0.15) / 100)
    (trade,) = result.trades
    assert trade.costs == pytest.approx(short.costs + cover.costs)
    assert trade.pnl == pytest.approx(-trade.costs)


def test_trade_pnl_is_net_of_costs_and_dividends():
    timeline = bars([[10.0], [10.0], [11.0], [12.0]], cash=[[0], [0], [0.5], [0]])
    script = Script({0: [("order_target_qty", 0, 100)], 3: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(us_costs(commission=US.commission, withholding=30), fill_timing="SAME_CLOSE"))
    (trade,) = result.trades
    assert (trade.side, trade.quantity, trade.entry_price, trade.exit_price, trade.exit_reason) == ("LONG", 100, 10, 12, "SIGNAL")
    assert trade.costs == pytest.approx(2.0)
    assert trade.pnl == pytest.approx(200 - 2 + 35)
    assert trade.return_pct == pytest.approx(233 / 1_000 * 100)
    assert result.cash[-1] == pytest.approx(10_000 + 233)


def test_partial_reductions_realize_pnl_at_the_average_cost():
    timeline = bars([[10.0], [20.0], [30.0], [5.0]])
    script = Script({0: [("order_target_qty", 0, 100)], 1: [("order_target_qty", 0, 200)],
                     2: [("order_target_qty", 0, 100)], 3: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(capital=100_000.0, fill_timing="SAME_CLOSE"))
    (trade,) = result.trades
    assert (trade.quantity, trade.entry_price, trade.exit_price) == (200, 15, 17.5)
    assert trade.pnl == pytest.approx((30 - 15) * 100 + (5 - 15) * 100)
    assert trade.return_pct == pytest.approx(500 / 3_000 * 100)


def test_a_reversal_closes_one_trade_and_opens_another():
    timeline = bars([[10.0]] * 4 + [[12.0]])
    result = simulate(timeline, direction([1], [1], [-1], [-1], [-1]), settings(
        us_costs(borrow=0.0), capital=1_000.0, allow_short=True, fill_timing="SAME_CLOSE"))
    long, short = result.trades
    assert (long.side, long.exit_reason, short.side, short.exit_reason) == ("LONG", "SIGNAL", "SHORT", "OPEN")
    assert (long.quantity, short.quantity) == (100, 100)
    assert [(f.side, f.quantity) for f in result.fills] == [("BUY", 100), ("SELL", 100), ("SELL", 100)]
    assert short.pnl == pytest.approx(-200) and short.exit_price == 12


# ----- AC-8: shorting -----


def test_shorts_need_allow_short():
    timeline = bars([[10.0]] * 3)
    result = simulate(timeline, direction([-1], [-1], [-1]), settings(fill_timing="SAME_CLOSE"))
    assert result.fills == () and result.flags == {"SHORTS_IGNORED": None}
    result = simulate(timeline, direction([-1], [-1], [-1]), settings(fill_timing="SAME_CLOSE", allow_short=True))
    assert result.quantity[0, 0] == -1_000 and result.flags == {}
    assert result.cash[0] == pytest.approx(20_000.0)  # the proceeds sit in cash


@pytest.mark.parametrize("market, quantity", [("US", -200), ("TW", -111)])
def test_short_sales_need_the_margin_share_of_buying_power(market, quantity):
    costs = us_costs() if market == "US" else tw_costs(taxes=None, short_fee=0.0)
    result = simulate(bars([[100.0]] * 2), weights([-3.0], [-3.0]), settings(costs, market, fill_timing="SAME_CLOSE", allow_short=True))
    assert result.quantity[0, 0] == quantity  # m × q × p ≤ 10,000 with m = 0.5 or 0.9


def test_buying_power_counts_open_shorts():
    timeline = bars([[100.0, 50.0]] * 3)
    script = Script({0: [("order_target_qty", 0, -100)], 1: [("order_target_qty", 1, 1_000)]})
    result = simulate(timeline, script, settings(us_costs(2, borrow=0.0), fill_timing="SAME_CLOSE", allow_short=True))
    # Cash 20,000 less (1 + 0.5) × 10,000 of short value leaves 5,000: 100 shares at 50.
    assert_array_equal(result.quantity[-1], [-100, 100])


def test_borrow_fees_accrue_by_calendar_days():
    days = [DAY0 + 3, DAY0 + 4, DAY0 + 7, DAY0 + 8]  # Thursday, Friday, Monday, Tuesday
    timeline = bars([[50.0], [60.0], [55.0], [55.0]], days=days)
    script = Script({0: [("order_target_qty", 0, -100)]})
    result = simulate(timeline, script, settings(us_costs(borrow=0.5), fill_timing="SAME_CLOSE", allow_short=True))
    first, weekend = 100 * 50 * 0.005 / 365, 100 * 60 * 0.005 * 3 / 365
    assert result.cash[1] == pytest.approx(10_000 + 5_000 - first)
    assert result.cash[2] == pytest.approx(10_000 + 5_000 - first - weekend)
    (trade,) = result.trades
    assert trade.costs == pytest.approx(first + weekend + 100 * 55 * 0.005 / 365)


def test_us_shorts_pay_dividends():
    timeline = bars([[50.0], [50.0], [49.0]], cash=[[0], [0], [1.0]])
    script = Script({0: [("order_target_qty", 0, -100)]})
    result = simulate(timeline, script, settings(us_costs(borrow=0.0, withholding=30), fill_timing="SAME_CLOSE", allow_short=True))
    assert result.cash[2] == pytest.approx(15_000 - 100)
    (trade,) = result.trades
    assert trade.pnl == pytest.approx(100 - 100)


def test_tw_short_sales_pay_the_short_sale_fee_and_the_tax():
    timeline = bars([[100.0], [100.0]])
    script = Script({0: [("order_target_qty", 0, -1_000)]})
    result = simulate(timeline, script, settings(tw_costs(commission=TW.commission), "TW", 1_000_000.0, fill_timing="SAME_CLOSE",
                                                 allow_short=True))
    assert result.fills[0].costs == pytest.approx(80 + 300 + 80)
    assert result.cash[0] == pytest.approx(1_100_000 - 460)


def test_a_suspension_forces_a_cover_and_stops_short_sales():
    timeline = bars([[100.0]] * 8, tradable=[[True]] * 3 + [[False]] + [[True]] * 4)
    suspension = ShortSuspension(DAY0 + 3, DAY0 + 5, "股東常會")
    script = Script({0: [("order_target_qty", 0, -1_000)], 4: [("order_target_qty", 0, -500)]})
    result = simulate(timeline, script, settings(tw_costs(taxes=None, short_fee=0.0), "TW", 1_000_000.0, allow_short=True,
                                                 suspensions=((suspension,),)))
    # Short at bar 1; the suspension starts at bar 3, untradable, so the cover fills at bar 4's open; the new short
    # sale of bar 4 waits through bar 5, the suspension's last day, and fills at bar 6.
    assert_array_equal(result.quantity[:, 0], [0, -1_000, -1_000, -1_000, 0, 0, -500, -500])
    first, second = result.trades
    assert (first.exit_reason, first.exit_time, second.entry_time) == ("FORCED_COVER", DAY0 + 4, DAY0 + 6)


# ----- AC-9: corporate actions -----


@pytest.mark.parametrize("held", [105, -105])
def test_a_share_event_settles_the_fraction_in_cash(held):
    timeline = bars([[10.0], [10.0], [20.0]], ratio=[[1], [1], [0.5]])
    script = Script({0: [("order_target_qty", 0, held)]})
    result = simulate(timeline, script, settings(capital=100_000.0, fill_timing="SAME_CLOSE", allow_short=True,
                                                 costs=us_costs(borrow=0.0)))
    assert result.quantity[2, 0] == np.sign(held) * 52
    lieu = np.sign(held) * 0.5 * 10 / 0.5
    assert result.cash[2] == pytest.approx(result.cash[1] + lieu)
    assert result.equity[2] == pytest.approx(result.equity[1])


def test_a_stock_dividend_scales_quantity_and_pending_targets():
    timeline = bars([[50.0], [50.0], [50 / 1.1], [50 / 1.1]], ratio=[[1], [1], [1.1], [1]])
    script = Script({0: [("order_target_qty", 0, 1_005)], 1: [("order_target_qty", 0, 2_000)]})
    result = simulate(timeline, script, settings(tw_costs(taxes=None), "TW", 1_000_000.0))
    # Bought 1,005 at bar 1's open; the order of bar 1 for 2,000 waits for bar 2's open, after the 1.1 ratio:
    # 1,005 becomes 1,105 and the target 2,200.
    assert_array_equal(result.quantity[:, 0], [0, 1_005, 2_200, 2_200])
    lieu = 0.5 * 50 / 1.1
    assert result.fills[1].quantity == 1_095
    assert result.cash[2] == pytest.approx(result.cash[1] + lieu - 1_095 * 50 / 1.1)


def test_cash_dividends_are_credited_net_of_withholding():
    timeline = bars([[10.0], [10.0], [9.0]], cash=[[0], [0], [1.0]])
    script = Script({0: [("order_target_qty", 0, 100)]})
    result = simulate(timeline, script, settings(us_costs(withholding=30), fill_timing="SAME_CLOSE"))
    assert result.cash[2] == pytest.approx(result.cash[1] + 70)


def test_a_us_ex_date_whose_open_equals_the_reference_keeps_equity():
    # Previous close 100, a 2:1 split and a 0.5 dividend: reference 100 / 2 − 0.5 = 49.5.
    timeline = bars([[100.0], [100.0], [49.5]], ratio=[[1], [1], [2]], cash=[[0], [0], [0.5]])
    script = Script({0: [("order_target_qty", 0, 50)]})
    result = simulate(timeline, script, settings(fill_timing="SAME_CLOSE"))
    assert result.quantity[2, 0] == 100
    assert result.equity[2] == pytest.approx(result.equity[1])
    assert result.cash[2] == pytest.approx(result.cash[1] + 50)


def test_a_tw_ex_date_whose_open_equals_the_reference_keeps_equity():
    # 權息 with P = 50, D = 2 and a ratio of 1.1: reference (50 − 2) / 1.1.
    reference = 48 / 1.1
    timeline = bars([[50.0], [50.0], [reference]], ratio=[[1], [1], [1.1]], cash=[[0], [0], [2.0]])
    script = Script({0: [("order_target_qty", 0, 1_000)]})
    result = simulate(timeline, script, settings(tw_costs(taxes=None), "TW", 1_000_000.0, fill_timing="SAME_CLOSE"))
    assert result.quantity[2, 0] == 1_100
    assert result.cash[2] == pytest.approx(result.cash[1] + 2_000)
    assert result.equity[2] == pytest.approx(result.equity[1])


def test_adjusted_mode_applies_no_action_and_matches_the_adjusted_return():
    raw = [[100.0], [100.0], [52.0], [54.0]]
    adjust = [[0.5], [0.5], [1.0], [1.0]]
    timeline = bars(raw, ratio=[[1], [1], [2], [1]], cash=[[0], [0], [0.5], [0]], adjust=adjust)
    result = simulate(timeline, weights([1.0], [np.nan], [np.nan], [np.nan]), settings(price_mode="ADJUSTED", fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[:, 0], [200, 200, 200, 200])
    assert result.cash[-1] == pytest.approx(0.0)
    assert result.equity[-1] / result.equity[0] == pytest.approx(54 / 50)
    assert_identity(timeline, result, adjusted=True)


# ----- AC-5's engine part: orders wait for a tradable bar -----


def test_an_order_waits_through_untradable_bars():
    timeline = bars([[10.0], [10.0], [np.nan], [11.0], [12.0]], tradable=[[True], [True], [False], [False], [True]])
    result = simulate(timeline, direction([0], [1], [1], [1], [1]), settings(capital=1_000.0))
    (buy,) = result.fills
    assert (buy.time, buy.price) == (DAY0 + 4, 12.0)


@pytest.mark.parametrize("lock, sell_bar, buy_bar", [(1, 1, 3), (-1, 2, 2)], ids=["locked up", "locked down"])
def test_a_limit_lock_blocks_only_its_direction(lock, sell_bar, buy_bar):
    # Locked up blocks buys, locked down blocks sells; the blocked order fills at the next bar.
    timeline = bars([[100.0]] * 5, lock=[[0], [lock], [0], [0], [0]])
    script = Script({0: [("order_target_qty", 0, 1_000)], 1: [("order_target_qty", 0, 0)]})
    result = simulate(timeline, script, settings(tw_costs(taxes=None), "TW", 1_000_000.0, fill_timing="SAME_CLOSE"))
    assert [(fill.side, fill.time) for fill in result.fills] == [("BUY", DAY0), ("SELL", DAY0 + sell_bar)]
    timeline = bars([[100.0]] * 5, lock=[[0], [0], [lock], [0], [0]])
    script = Script({1: [("order_target_qty", 0, 1_000)]})
    result = simulate(timeline, script, settings(tw_costs(taxes=None), "TW", 1_000_000.0))
    assert [(fill.side, fill.time) for fill in result.fills] == [("BUY", DAY0 + buy_bar)]


# ----- strategy outputs and sizing (§9.1, §9.4) -----


def test_direction_entries_hold_their_quantity():
    timeline = bars([[10.0, 20.0], [10.0, 20.0], [20.0, 20.0], [20.0, 20.0]])
    result = simulate(timeline, direction([1, 1], [1, 1], [1, 1], [1, 1]), settings(us_costs(2), fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[:, 0], [500, 500, 500, 500])  # 1/N of equity, no drift rebalancing
    assert_array_equal(result.quantity[:, 1], [250, 250, 250, 250])


@pytest.mark.parametrize(
    "sizing, maximum, quantity",
    [(Sizing(fraction=0.3), 100, 300), (Sizing("FIXED_AMOUNT", amount=2_500), 100, 250), (Sizing("FIXED_SHARES", shares=42), 100, 42),
     (Sizing("FIXED_SHARES", shares=900), 50, 500), (Sizing("VOL_RISK", risk_pct=1.0, atr_n=2, atr_mult=2.0), 100, 50)],
)
def test_each_sizing_mode_and_the_cap(sizing, maximum, quantity):
    # Raw bars range 1 a day and the adjusted ones 0.5: the ATR of 0.5 becomes 1 in the raw scale, so VOL_RISK buys
    # 1% × 10,000 / (2 × 1) = 50.
    timeline = bars([[10.0]] * 4, highs=[[10.5]] * 4, lows=[[9.5]] * 4, adjust=[[0.5]] * 4)
    result = simulate(timeline, direction([0], [0], [1], [1]), settings(sizing=sizing, max_position_pct=maximum, fill_timing="SAME_CLOSE"))
    assert result.quantity[-1, 0] == quantity


def test_weights_trade_on_changes_and_rebalance_bars():
    timeline = bars([[10.0], [20.0], [20.0], [10.0]])
    held = simulate(timeline, weights([0.5], [0.5], [0.5], [0.5]), settings(fill_timing="SAME_CLOSE"))
    assert_array_equal(held.quantity[:, 0], [500, 500, 500, 500])
    rebalanced = simulate(timeline, weights([0.5], [0.5], [0.5], [0.5], rebalance=[False, True, False, True]),
                          settings(fill_timing="SAME_CLOSE"))
    # Equity 15,000 at bar 1: 0.5 of it is 375 shares at 20; at bar 3 equity is 11,250: 562 shares at 10.
    assert_array_equal(rebalanced.quantity[:, 0], [500, 375, 375, 562])


def test_the_book_places_every_kind_of_order_and_the_last_one_wins():
    timeline = bars([[10.0, 20.0]] * 4)
    script = Script({
        0: [("order_target_qty", 0, 100), ("order_target_qty", 0, 120), ("order_value", 1, 1_000)],
        1: [("order_qty", 0, -20), ("order_target_weight", 1, 0.25)],
        2: [("order_target_qty", 1, -5)],
    })
    result = simulate(timeline, script, settings(us_costs(2), fill_timing="SAME_CLOSE"))
    assert_array_equal(result.quantity[:, 0], [120, 100, 100, 100])
    assert_array_equal(result.quantity[:, 1], [50, 125, 0, 0])  # a short without allow_short counts as 0
    assert result.flags == {"SHORTS_IGNORED": None}


def test_a_position_whose_data_end_stays_at_its_last_close():
    timeline = bars([[10.0], [10.0], [11.0], [np.nan], [np.nan]])
    result = simulate(timeline, direction([1], [1], [1], [1], [0]), settings(capital=1_000.0, fill_timing="SAME_CLOSE"))
    assert result.flags == {"DATA_ENDED": None}
    assert_allclose(result.equity[2:], [1_100.0] * 3)
    (trade,) = result.trades
    assert (trade.exit_reason, trade.exit_price) == ("OPEN", 11.0)


def test_warm_up_bars_do_not_trade():
    timeline = bars([[10.0]] * 5, start=2)
    result = simulate(timeline, direction([1], [1], [1], [1], [1]), settings(capital=1_000.0, fill_timing="SAME_CLOSE"))
    assert result.fills[0].time == DAY0 + 2
    assert_array_equal(result.time, np.arange(DAY0 + 2, DAY0 + 5))


def test_two_runs_are_identical():
    rng = np.random.default_rng(11)
    closes = 30 * np.exp(np.cumsum(rng.normal(0, 0.02, size=(60, 3)), axis=0))
    timeline = bars(closes, closes * 1.001, ratio=np.where(rng.uniform(size=closes.shape) < 0.02, 2.0, 1.0))
    targets = direction(*np.sign(rng.normal(size=(60, 3))))
    run = lambda: simulate(timeline, targets, settings(us_costs(3, US.commission, slippage=5), allow_short=True))  # noqa: E731
    first, second = run(), run()
    for name in ("equity", "cash", "quantity", "long_value", "short_value"):
        assert getattr(first, name).tobytes() == getattr(second, name).tobytes()
    assert first.trades == second.trades and first.fills == second.fills


def test_the_end_of_a_run_reports_holdings_and_pending_targets():
    timeline = bars([[10.0, 20.0]] * 3)
    result = simulate(timeline, direction([1, 0], [1, 0], [1, 1]), settings(us_costs(2)))
    assert_array_equal(result.holdings, [500, 0])
    assert result.pending == {1: 250}
