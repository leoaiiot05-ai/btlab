"""Indicators against hand-computed values (SPEC.md §8.1, AC-6)."""

import math
import statistics

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from btlab import indicators
from btlab.indicators import Inputs

NAN = math.nan


def col(*values: float) -> np.ndarray:
    """One symbol as a bars × 1 array."""
    return np.array(values, dtype=np.float64)[:, None]


def values(array: np.ndarray) -> list[float]:
    return array[:, 0].tolist()


def assert_values(actual: np.ndarray, expected: list[float]) -> None:
    assert_allclose(actual[:, 0], expected, rtol=1e-12, atol=1e-12, equal_nan=True)


def test_sma_is_the_mean_of_the_last_n_values():
    assert_values(indicators.sma(col(1, 2, 3, 4, 5), 3), [NAN, NAN, 2, 3, 4])
    assert_values(indicators.sma(col(1, 2), 3), [NAN, NAN])


def test_ema_is_seeded_with_the_sma():
    # α = 0.5: seed (2 + 4 + 6) / 3 = 4, then 0.5 × 8 + 0.5 × 4 = 6, then 0.5 × 20 + 0.5 × 6 = 13.
    assert_values(indicators.ema(col(2, 4, 6, 8, 20), 3), [NAN, NAN, 4, 6, 13])


def test_rsi_uses_wilders_seed_and_smoothing():
    # Changes 1, 1, −1, 2, 0: the first averages are 2/3 and 1/3 (RS 2); then gain 10/9, loss 2/9 (RS 5); then 20/27, 4/27.
    assert_values(indicators.rsi(col(10, 11, 12, 11, 13, 13), 3), [NAN, NAN, NAN, 100 - 100 / 3, 100 - 100 / 6, 100 - 100 / 6])


def test_rsi_without_losses_is_100_and_without_any_change_50():
    assert_values(indicators.rsi(col(1, 2, 3, 4), 2), [NAN, NAN, 100, 100])
    assert_values(indicators.rsi(col(5, 5, 5, 5), 2), [NAN, NAN, 50, 50])


def test_macd_signal_is_seeded_with_the_sma_of_its_first_defined_values():
    close = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    fast, slow = [NAN, 1.5], [NAN, NAN, 7 / 3]
    for price in close[2:]:
        fast.append(2 / 3 * price + 1 / 3 * fast[-1])
    for price in close[3:]:
        slow.append(0.5 * price + 0.5 * slow[-1])
    line = [f - s for f, s in zip(fast, slow)]
    signal = [NAN, NAN, NAN, (line[2] + line[3]) / 2]
    for value in line[4:]:
        signal.append(2 / 3 * value + 1 / 3 * signal[-1])
    result = indicators.macd(col(*close), 2, 3, 2)
    assert_values(result["macd"], line)
    assert_values(result["signal"], signal)
    assert_values(result["hist"], [m - s for m, s in zip(line, signal)])


def test_bbands_use_the_population_deviation():
    result = indicators.bbands(col(1, 2, 3, 4), 3, 2.0)
    sd = math.sqrt(2 / 3)
    assert_values(result["middle"], [NAN, NAN, 2, 3])
    assert_values(result["upper"], [NAN, NAN, 2 + 2 * sd, 3 + 2 * sd])
    assert_values(result["lower"], [NAN, NAN, 2 - 2 * sd, 3 - 2 * sd])
    assert_values(result["pctb"], [NAN, NAN, (3 - (2 - 2 * sd)) / (4 * sd), (4 - (3 - 2 * sd)) / (4 * sd)])
    assert_values(result["width"], [NAN, NAN, 4 * sd / 2, 4 * sd / 3])
    flat = indicators.bbands(col(5, 5, 5), 3, 2.0)
    assert_values(flat["pctb"], [NAN, NAN, NAN])
    assert_values(flat["width"], [NAN, NAN, 0])


def test_atr_starts_with_the_high_low_range_and_smooths_like_wilder():
    # True ranges 2, max(2, 6, 4) = 6, max(2, 2, 4) = 4, max(2, 0, 2) = 2; seed (2 + 6) / 2 = 4, then (4 + 4) / 2, (4 + 2) / 2.
    high, low, close = col(10, 15, 12, 11), col(8, 13, 10, 9), col(9, 14, 11, 10)
    assert_values(indicators.atr(high, low, close, 2), [NAN, 4, 4, 3])


def test_donchian_excludes_the_current_bar_and_highest_includes_it():
    high, low = col(1, 3, 2, 5, 4), col(0, 2, 1, 4, 3)
    channel = indicators.donchian(high, low, 2)
    assert_values(channel["upper"], [NAN, NAN, 3, 3, 5])
    assert_values(channel["lower"], [NAN, NAN, 0, 1, 1])
    assert_values(channel["middle"], [NAN, NAN, 1.5, 2, 3])
    assert_values(indicators.highest(high, 2), [NAN, 3, 3, 5, 5])
    assert_values(indicators.lowest(low, 2), [NAN, 0, 1, 1, 3])


def test_roc():
    # A division by zero (0 two bars back) is NaN.
    assert_values(indicators.roc(col(10, 11, 12, 15, 0, 3, 6), 2), [NAN, NAN, 0.2, 15 / 11 - 1, -1, 3 / 15 - 1, NAN])


def test_zscore_is_nan_when_the_deviation_is_zero():
    sd = math.sqrt(2 / 3)
    assert_values(indicators.zscore(col(1, 2, 3, 4), 3), [NAN, NAN, 1 / sd, 1 / sd])
    assert_values(indicators.zscore(col(0.1, 0.1, 0.1, 0.2), 3), [NAN, NAN, NAN, (0.2 - 0.4 / 3) / statistics.pstdev([0.1, 0.1, 0.2])])


def test_stdev_is_the_population_and_volatility_the_sample_deviation():
    close = [100.0, 110.0, 99.0, 108.9, 107.0]
    assert_values(indicators.stdev(col(*close), 3)[:, :], [NAN, NAN, statistics.pstdev(close[:3]), statistics.pstdev(close[1:4]),
                                                           statistics.pstdev(close[2:])])
    returns = [math.log(b / a) for a, b in zip(close, close[1:])]
    expected = [NAN, NAN, NAN, statistics.stdev(returns[:3]) * math.sqrt(252), statistics.stdev(returns[1:]) * math.sqrt(252)]
    assert_values(indicators.volatility(col(*close), 3, 252), expected)
    assert_values(indicators.volatility(col(*close), 3, 252 * 390), [v * math.sqrt(390) for v in expected])


def test_vwap_restarts_each_session():
    session = np.array([0, 0, 0, 1, 1])
    high, low, close = col(11, 12, 13, 21, 23), col(9, 10, 11, 19, 21), col(10, 11, 12, 20, 22)
    volume = col(100, 300, 0, 50, 50)
    assert_values(indicators.vwap(high, low, close, volume, session), [10, 10.75, 10.75, 20, 21])
    assert_values(indicators.vwap(high, low, close, col(0, 0, 0, 0, 10), session), [NAN, NAN, NAN, NAN, 22])


def test_price_reads_its_source():
    inputs = Inputs(col(1), col(2), col(3), col(4), col(5))
    assert [values(indicators.price(inputs, src))[0] for src in indicators.SOURCES] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "compute, warmup",
    [
        (lambda x: indicators.sma(x, 3), 2),
        (lambda x: indicators.ema(x, 3), 2),
        (lambda x: indicators.rsi(x, 3), 3),
        (lambda x: indicators.stdev(x, 3), 2),
        (lambda x: indicators.roc(x, 3), 3),
        (lambda x: indicators.highest(x, 3), 2),
        (lambda x: indicators.volatility(x, 3, 252), 3),
        (lambda x: indicators.macd(x, 2, 3, 2)["signal"], 3),
    ],
)
def test_warm_up_and_a_symbol_listed_later(compute, warmup):
    data = np.array([[10.0 + i + (i % 3) for i in range(12)], [NAN] * 4 + [20.0 + i % 4 for i in range(8)]]).T
    result = compute(data)
    assert np.isnan(result[:warmup, 0]).all() and np.isfinite(result[warmup:, 0]).all()
    assert np.isnan(result[: 4 + warmup, 1]).all() and np.isfinite(result[4 + warmup:, 1]).all()
    assert_array_equal(compute(data[:, 1:])[:, 0], result[:, 1])


def test_a_window_holding_nan_is_nan_and_recursions_restart_after_it():
    x = col(1, 2, 3, NAN, 4, 5, 6)
    assert_values(indicators.sma(x, 2), [NAN, 1.5, 2.5, NAN, NAN, 4.5, 5.5])
    assert_values(indicators.ema(x, 2), [NAN, 1.5, 2 / 3 * 3 + 1 / 3 * 1.5, NAN, NAN, 4.5, 2 / 3 * 6 + 1 / 3 * 4.5])
    assert_values(indicators.highest(x, 2), [NAN, 2, 3, NAN, NAN, 5, 6])
    assert_values(indicators.stdev(x, 2), [NAN, 0.5, 0.5, NAN, NAN, 0.5, 0.5])


def test_symbols_are_independent_columns():
    rng = np.random.default_rng(3)
    data = 100 + rng.normal(size=(40, 3)).cumsum(axis=0)
    both = indicators.rsi(data, 5)
    for j in range(3):
        assert_array_equal(indicators.rsi(data[:, j : j + 1], 5)[:, 0], both[:, j])
