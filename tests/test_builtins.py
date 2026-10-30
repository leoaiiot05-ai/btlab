"""The built-in strategies A to J and sizing through the engine (SPEC.md §9.1 to §9.4, AC-12)."""

import dataclasses
import math

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from btlab import indicators
from btlab.builtins import BUILTINS, View
from btlab.engine import Targets, simulate
from btlab.indicators import Inputs
from btlab.sizing import Sizing
from builders import DAY0, bars, settings, us_costs

NAN = math.nan


def view(closes, highs=None, lows=None, *, days=None, rf=None, start=0, symbols=None) -> View:
    close = np.array(closes, dtype=np.float64)
    if close.ndim == 1:
        close = close[:, None]
    high = close if highs is None else np.array(highs, dtype=np.float64).reshape(close.shape)
    low = close if lows is None else np.array(lows, dtype=np.float64).reshape(close.shape)
    days = np.arange(DAY0, DAY0 + len(close)) if days is None else np.array(days)
    return View(symbols or tuple("ABCDEFG"[: close.shape[1]]), days, Inputs(close, high, low, close, np.full(close.shape, 1e3)),
                np.zeros(len(close)) if rf is None else np.array(rf, dtype=np.float64), start)


def run(builtin_id: str, data: View, **params):
    builtin = BUILTINS[builtin_id]
    defaults = {name: param.default for name, param in builtin.schema("US", data.symbols).items()}
    return builtin.run(data, defaults | params)


def directions(builtin_id: str, closes, **params) -> list[float]:
    return run(builtin_id, view(closes), **params).values[:, 0].tolist()


def assert_directions(found: list[float], expected: list[float]) -> None:
    assert_allclose(found, expected, equal_nan=True)


# ----- DIRECTION strategies -----


@pytest.mark.parametrize("short, expected", [(False, [NAN, NAN, 1, 1, 1, 0, 0]), (True, [NAN, NAN, 1, 1, 1, -1, -1])])
def test_ma_cross_is_long_while_the_fast_average_is_above(short, expected):
    assert_directions(directions("MA_CROSS", [1, 2, 3, 4, 3, 2, 1], fast=2, slow=3, allow_short=short), expected)


def test_ma_cross_can_use_the_ema():
    found = run("MA_CROSS", view([1, 2, 3, 4, 3, 2, 1, 1, 1]), fast=2, slow=3, ma="EMA").values[:, 0]
    close = np.array([1, 2, 3, 4, 3, 2, 1, 1, 1.0])[:, None]
    expected = np.where(indicators.ema(close, 2) > indicators.ema(close, 3), 1.0, 0.0)[:, 0]
    assert_directions(found[2:].tolist(), expected[2:].tolist())


@pytest.mark.parametrize("short, expected", [(False, [NAN, NAN, 1, 1, 0, 0, 1]), (True, [NAN, NAN, 1, 1, -1, -1, 1])])
def test_rsi_enters_below_lower_and_exits_above_upper(short, expected):
    # RSI(2) is 0, 50, 75, 37.5 and 18.75 from bar 2: the long exits at bar 4, where a short enters (a reversal); at
    # bar 6 the short exits and the long enters again.
    assert_directions(directions("RSI", [10, 9, 8, 9, 10, 9, 8], n=2, allow_short=short), expected)


@pytest.mark.parametrize("short, expected", [(False, [NAN, NAN, NAN, 0, 0, 1, 0]), (True, [NAN, NAN, NAN, -1, -1, 1, -1])])
def test_macd_follows_its_signal_line(short, expected):
    assert_directions(directions("MACD", [10, 11, 12, 11, 10, 12, 11], fast=2, slow=3, signal=2, allow_short=short), expected)


@pytest.mark.parametrize("short, expected", [(False, [NAN, NAN, 0, 1, 0, 0, 0, 1, 1]), (True, [NAN, NAN, 0, 1, 0, 0, -1, 1, 1])])
def test_bollinger_enters_outside_the_bands_and_exits_at_the_middle(short, expected):
    # Bar 3 closes below the lower band; bar 4 above the middle; bar 6 above the upper band; bar 7 below the middle and
    # the lower band at once, which reverses a short into a long.
    assert_directions(directions("BOLLINGER", [10, 10, 10, 7, 10, 11, 13, 10, 9], n=3, k=1.0, allow_short=short), expected)


@pytest.mark.parametrize("short, expected", [(False, [NAN] * 3 + [1, 1, 0, 0, 0, 0]), (True, [NAN] * 3 + [1, 1, 0, -1, -1, -1])])
def test_breakout_enters_on_the_entry_channel_and_exits_on_the_exit_channel(short, expected):
    assert_directions(directions("BREAKOUT", [10, 11, 12, 13, 14, 12, 11, 8, 7], entry_n=3, exit_n=2, allow_short=short), expected)


@pytest.mark.parametrize("short, expected", [(False, [NAN, NAN, 0, 1, 0, 0, 0, 0, 0]), (True, [NAN, NAN, 0, 1, 0, 0, -1, 0, 0])])
def test_zscore_reversion(short, expected):
    # z is −0.71, −1.40, 0.71, 0.86, 1.40, −0.89 and 0 from bar 2, with entry 1 and exit 0.5.
    assert_directions(directions("ZSCORE_REVERSION", [10, 10.5, 10, 7, 10, 10.5, 13, 10, 11.5], n=3, entry_z=1.0, exit_z=0.5,
                                 allow_short=short), expected)


def test_a_symbol_without_data_gets_no_instruction():
    data = view(np.array([[10, NAN], [11, NAN], [12, 20], [11, 21], [10, 22]]))
    found = run("MA_CROSS", data, fast=2, slow=3).values
    assert_directions(found[:, 1].tolist(), [NAN, NAN, NAN, NAN, 1])


# ----- WEIGHTS and ORDERS strategies -----


def test_buy_hold_weights_the_symbols_with_data_on_the_first_bar():
    data = view(np.array([[10, 20, NAN]] * 3 + [[10, 20, 30]] * 2), start=1)
    targets = run("BUY_HOLD", data)
    assert targets.kind == "WEIGHTS" and targets.rebalance is None
    assert_allclose(targets.values, [[NAN] * 3, [0.5, 0.5, 0.0], [NAN] * 3, [NAN] * 3, [NAN] * 3], equal_nan=True)


# Weekdays of 2020: May 25 to 29, June 1 to 5, June 29 and 30, July 1 and 2.
MONTH_DAYS = [DAY0 + offset for offset in (-7, -6, -5, -4, -3, 0, 1, 2, 3, 4, 28, 29, 30, 31)]


def test_dca_buys_on_period_starts_while_the_cash_lasts():
    timeline = bars([[100.0, 50.0]] * len(MONTH_DAYS), days=MONTH_DAYS)
    data = view([[100.0, 50.0]] * len(MONTH_DAYS), days=MONTH_DAYS)
    strategy = run("DCA", data, amount=1_000.0)
    result = simulate(timeline, strategy, settings(us_costs(2, borrow=0.0), capital=1_200.0, fill_timing="SAME_CLOSE"))
    # The first bar and the first bar of June buy 500 of each; July's first buy needs 500 with 200 left, so it stops.
    assert [(fill.time, fill.symbol, fill.quantity) for fill in result.fills] == [
        (MONTH_DAYS[0], "A", 5), (MONTH_DAYS[0], "B", 10)]
    rich = simulate(timeline, strategy, settings(us_costs(2, borrow=0.0), capital=10_000.0, fill_timing="SAME_CLOSE"))
    assert [fill.time for fill in rich.fills] == [MONTH_DAYS[0]] * 2 + [DAY0] * 2 + [MONTH_DAYS[12]] * 2
    assert all(fill.side == "BUY" for fill in rich.fills)


def test_weekly_dca_and_a_symbol_without_data():
    days = [DAY0 + offset for offset in range(10) if offset % 7 < 5]
    timeline = bars([[100.0, NAN]] * len(days), days=days)
    data = view([[100.0, NAN]] * len(days), days=days)
    result = simulate(timeline, run("DCA", data, amount=500.0, frequency="weekly"),
                      settings(us_costs(2, borrow=0.0), fill_timing="SAME_CLOSE"))
    assert [(fill.time, fill.symbol, fill.quantity) for fill in result.fills] == [(DAY0, "A", 5), (DAY0 + 7, "A", 5)]


def rotation_view(**options) -> View:
    """Weekly bars of A (fast up), B (slow up), C (down) and D (no data)."""
    count = 12
    t = np.arange(count, dtype=float)
    closes = np.column_stack([100 * 1.05 ** t, 100 * 1.01 ** t, 100 * 0.97 ** t, np.full(count, NAN)])
    return view(closes, days=[DAY0 + 7 * week for week in range(count)], **options)


def test_momentum_rotation_with_fewer_candidates_than_slots():
    targets = run("MOMENTUM_ROTATION", rotation_view(), lookback=2, top_k=3, rebalance="weekly")
    assert targets.rebalance.all()
    assert_allclose(targets.values[:2], np.zeros((2, 4)))  # no score yet: all in cash
    assert_allclose(targets.values[5], [1 / 3, 1 / 3, 0, 0])  # C is negative, D has no data: one slot stays in cash
    without_filter = run("MOMENTUM_ROTATION", rotation_view(), lookback=2, top_k=3, rebalance="weekly", abs_filter=False)
    assert_allclose(without_filter.values[5], [1 / 3, 1 / 3, 1 / 3, 0])
    top_one = run("MOMENTUM_ROTATION", rotation_view(), lookback=2, top_k=1, rebalance="weekly")
    assert_allclose(top_one.values[5], [1, 0, 0, 0])


def test_momentum_rotation_measures_the_score_skip_bars_back():
    closes = np.column_stack([[100, 100, 100, 130, 100, 100.0], [100, 100, 100, 100, 120, 120.0]])
    data = view(closes, days=[DAY0 + 7 * week for week in range(6)])
    now = run("MOMENTUM_ROTATION", data, lookback=1, top_k=1, rebalance="weekly", abs_filter=False)
    skipped = run("MOMENTUM_ROTATION", data, lookback=1, top_k=1, rebalance="weekly", abs_filter=False, skip=1)
    assert_allclose(now.values[4], [0, 1])  # at bar 4: A fell, B rose
    assert_allclose(skipped.values[4], [1, 0])  # measured at bar 3: A rose


def test_vol_inverse_weights_sum_to_selected_over_top_k():
    rng = np.random.default_rng(5)
    count = 80
    noise = np.column_stack([rng.normal(0.004, sd, count) for sd in (0.01, 0.03, 0.02)])
    closes = 100 * np.exp(np.cumsum(noise, axis=0))
    closes[:, 2] = 100 * np.exp(np.cumsum(-np.abs(noise[:, 2])))  # always falling: never selected
    data = view(np.column_stack([closes, np.full(count, NAN)]), days=[DAY0 + 7 * week for week in range(count)])
    targets = run("MOMENTUM_ROTATION", data, lookback=20, top_k=3, rebalance="weekly", weighting="vol_inverse")
    row = targets.values[70]
    assert row.sum() == pytest.approx(2 / 3)
    volatility = indicators.volatility(data.bars.close, 60, 252)[70]
    assert row[0] / row[1] == pytest.approx(volatility[1] / volatility[0])
    assert row[2] == 0 and row[3] == 0


def test_dual_momentum_picks_the_best_risk_symbol_or_the_safe_asset():
    months = [DAY0 + offset for offset in (0, 31, 61, 92, 123, 153)]  # first days of six months
    closes = np.column_stack([[100, 110, 121, 100, 90, 95.0], [100, 105, 110, 115, 100, 90.0], [50, 50, 50, 50, 50, 50.0]])
    data = view(closes, days=months, symbols=("SPY", "EFA", "TLT"))
    targets = run("DUAL_MOMENTUM", data, lookback=2, safe="TLT")
    assert targets.rebalance.all()
    assert_allclose(targets.values[2], [1, 0, 0])  # SPY rose most
    assert_allclose(targets.values[4], [0, 0, 1])  # both fell: the safe asset
    cash = run("DUAL_MOMENTUM", data, lookback=2, safe="")
    assert_allclose(cash.values[4], [0, 0, 0])
    expensive_cash = view(closes, days=months, symbols=("SPY", "EFA", "TLT"), rf=[5_000.0] * 6)
    assert_allclose(run("DUAL_MOMENTUM", expensive_cash, lookback=2, safe="TLT").values[2], [0, 0, 1])


def test_rotations_never_short():
    for builtin_id in ("MOMENTUM_ROTATION", "DUAL_MOMENTUM", "BUY_HOLD"):
        values = run(builtin_id, rotation_view(), lookback=2).values if builtin_id != "BUY_HOLD" else run(builtin_id, rotation_view()).values
        assert not (values < 0).any(), builtin_id


# ----- truncation invariance (§9.7) -----


def random_view(rows: int, seed: int) -> View:
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, size=(rows, 3)), axis=0))
    closes[:9, 2] = NAN
    spread = rng.uniform(0, 0.02, size=closes.shape)
    days = np.array([DAY0 + i + 2 * (i // 5) for i in range(rows)])  # weekdays
    return view(closes, closes * (1 + spread), closes * (1 - spread), days=days, rf=np.linspace(1, 4, rows), start=5)


def cut(data: View, rows: int) -> View:
    bars_ = Inputs(*(getattr(data.bars, name)[:rows] for name in ("open", "high", "low", "close", "volume")))
    return dataclasses.replace(data, days=data.days[:rows], bars=bars_, rf=data.rf[:rows])


def altered(data: View, rows: int) -> View:
    other = random_view(len(data.days), seed=99)
    bars_ = Inputs(*(np.concatenate([getattr(data.bars, name)[:rows], getattr(other.bars, name)[rows:]])
                     for name in ("open", "high", "low", "close", "volume")))
    return dataclasses.replace(data, bars=bars_)


SMALL = {
    "MA_CROSS": {"fast": 3, "slow": 8, "allow_short": True},
    "RSI": {"n": 5, "allow_short": True},
    "MACD": {"fast": 3, "slow": 6, "signal": 3, "allow_short": True},
    "BOLLINGER": {"n": 6, "k": 1.0, "allow_short": True},
    "BREAKOUT": {"entry_n": 6, "exit_n": 3, "allow_short": True},
    "BUY_HOLD": {},
    "MOMENTUM_ROTATION": {"lookback": 10, "skip": 2, "rebalance": "weekly", "weighting": "vol_inverse", "abs_filter": False},
    "DUAL_MOMENTUM": {"lookback": 10},
    "ZSCORE_REVERSION": {"n": 6, "entry_z": 1.0, "exit_z": 0.2, "allow_short": True},
}


@pytest.mark.parametrize("builtin_id", sorted(SMALL))
@pytest.mark.parametrize("c", [20, 61])
def test_bars_after_c_change_no_target_up_to_c(builtin_id, c):
    data = random_view(90, seed=1)
    full = run(builtin_id, data, **SMALL[builtin_id])
    assert np.isfinite(full.values[: c + 1]).any()
    for other in (run(builtin_id, cut(data, c + 1), **SMALL[builtin_id]), run(builtin_id, altered(data, c + 1), **SMALL[builtin_id])):
        assert_array_equal(other.values[: c + 1], full.values[: c + 1])
        if full.rebalance is not None:
            assert_array_equal(other.rebalance[: c + 1], full.rebalance[: c + 1])


@pytest.mark.parametrize("c", [20, 61])
def test_dca_fills_up_to_c_ignore_later_bars(c):
    data = random_view(90, seed=2)
    timeline = bars(data.bars.close, days=data.days)
    run_on = lambda d, t: simulate(t, run("DCA", d, amount=2_000.0, frequency="weekly"),  # noqa: E731
                                   settings(us_costs(3, borrow=0.0), capital=100_000.0))
    full = run_on(data, timeline)
    short = run_on(cut(data, c + 1), bars(data.bars.close[: c + 1], days=data.days[: c + 1]))
    early = [fill for fill in full.fills if fill.time <= data.days[c]]
    assert early and early == [fill for fill in short.fills if fill.time <= data.days[c]]


# ----- sizing through the engine (§9.4) -----


@pytest.mark.parametrize(
    "sizing, cap, quantity",
    [
        (Sizing(), 100, 50),  # 1/N of equity: 5,000 at 100
        (Sizing(fraction=0.3), 100, 30),
        (Sizing("FIXED_AMOUNT", amount=2_500), 100, 25),
        (Sizing("FIXED_SHARES", shares=70), 100, 70),
        (Sizing("FIXED_SHARES", shares=70), 40, 40),  # capped at 40% of equity
        (Sizing("VOL_RISK", risk_pct=1.0, atr_n=3, atr_mult=2.0), 100, 25),  # 1% × 10,000 / (2 × ATR 2)
    ],
)
def test_ma_cross_entries_in_every_sizing_mode(sizing, cap, quantity):
    # The fast average crosses above at bar 4, whose close is 100; every true range is 2.
    close = np.array([100.0, 99, 98, 99, 100, 102, 103, 120, 140])
    closes = np.column_stack([close, np.full(len(close), 50.0)])
    timeline = bars(closes, highs=closes + 1, lows=closes - 1)
    data = view(closes, closes + 1, closes - 1)
    targets = run("MA_CROSS", data, fast=2, slow=3)
    result = simulate(timeline, targets, settings(us_costs(2, borrow=0.0), sizing=sizing, max_position_pct=cap,
                                                  fill_timing="SAME_CLOSE"))
    held = result.quantity[:, 0]
    entry = int(np.flatnonzero(held)[0])
    assert held[entry] == quantity
    assert (held[entry:] == quantity).all()  # held without drift rebalancing as the price climbs
