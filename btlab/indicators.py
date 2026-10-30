"""Indicators on bars × symbols arrays (SPEC.md §8.1). Pure.

Every indicator is causal: its value at bar t reads bars up to and including t. Values are NaN until enough data exist,
and a window that holds NaN gives NaN. The recursive ones (EMA, MACD's signal, the Wilder averages of RSI and ATR) seed
again after a NaN, so a symbol listed mid-period seeds at its own start.
"""

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SOURCES = ("open", "high", "low", "close", "volume")
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, eq=False)
class Inputs:
    """The adjusted bars of one timeline, as indicators read them (§8.1, §10.2), each bars × symbols.

    periods_per_year is 252 on the daily timeline and 252 × bars per regular session on minute timelines. Minute
    timelines add session (each bar's session index), real (the symbol has a bar at that time) and minutes (from the
    session's open to each bar's end).
    """

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    periods_per_year: float = TRADING_DAYS_PER_YEAR
    session: np.ndarray | None = None
    real: np.ndarray | None = None
    minutes: np.ndarray | None = None


def price(inputs: Inputs, src: str = "close") -> np.ndarray:
    return getattr(inputs, src)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """The mean of the last n values. Prefix sums make each value depend on bars up to its own only."""
    x = np.asarray(x, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    if n > len(x):
        return out
    missing = np.isnan(x)
    zero = np.zeros((1, *x.shape[1:]))
    sums = np.concatenate([zero, np.cumsum(np.where(missing, 0.0, x), axis=0)])
    gaps = np.concatenate([zero, np.cumsum(missing, axis=0)])
    out[n - 1:] = np.where(gaps[n:] - gaps[:-n] == 0, (sums[n:] - sums[:-n]) / n, np.nan)
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """α = 2 / (n + 1), seeded with the SMA of the first n values."""
    return _recursive(x, n, 2 / (n + 1))


def rsi(close: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RSI of the changes of close: 100 when the average loss is 0 and the gain is not, 50 when both are."""
    change = close - shift(close, 1)
    gain = _recursive(np.maximum(change, 0.0), n, 1 / n)
    loss = _recursive(np.maximum(-change, 0.0), n, 1 / n)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100 - 100 / (1 + gain / loss)
    value = np.where(loss == 0, np.where(gain == 0, 50.0, 100.0), value)
    return np.where(np.isnan(gain) | np.isnan(loss), np.nan, value)


def macd(close: np.ndarray, fast: int, slow: int, signal: int) -> dict[str, np.ndarray]:
    """macd = EMA(fast) − EMA(slow); signal = its EMA, seeded with the SMA of its first `signal` defined values."""
    line = ema(close, fast) - ema(close, slow)
    trigger = _recursive(line, signal, 2 / (signal + 1))
    return {"macd": line, "signal": trigger, "hist": line - trigger}


def bbands(close: np.ndarray, n: int, k: float) -> dict[str, np.ndarray]:
    """Bollinger bands with the population deviation."""
    middle = sma(close, n)
    deviation = stdev(close, n)
    upper, lower = middle + k * deviation, middle - k * deviation
    return {
        "upper": upper, "middle": middle, "lower": lower,
        "pctb": _ratio(close - lower, upper - lower), "width": _ratio(upper - lower, middle),
    }


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """Wilder's average of the true range, seeded with the mean of the first n; H − L on a bar without a previous close."""
    previous = shift(close, 1)
    spread = high - low
    true_range = np.fmax(spread, np.fmax(np.abs(high - previous), np.abs(low - previous)))
    return _recursive(np.where(np.isnan(spread), np.nan, true_range), n, 1 / n)


def donchian(high: np.ndarray, low: np.ndarray, n: int) -> dict[str, np.ndarray]:
    """The channel of the n bars before t, without bar t, so that close > upper is a breakout (intentional)."""
    upper, lower = shift(highest(high, n), 1), shift(lowest(low, n), 1)
    return {"upper": upper, "lower": lower, "middle": (upper + lower) / 2}


def highest(x: np.ndarray, n: int) -> np.ndarray:
    """Over the last n bars, including t."""
    return _extreme(x, n, np.max)


def lowest(x: np.ndarray, n: int) -> np.ndarray:
    return _extreme(x, n, np.min)


def roc(x: np.ndarray, n: int) -> np.ndarray:
    return _ratio(x, shift(x, n)) - 1


def stdev(x: np.ndarray, n: int) -> np.ndarray:
    """The population standard deviation over n bars."""
    return np.sqrt(_variance(x, n, ddof=0))


def zscore(x: np.ndarray, n: int) -> np.ndarray:
    """(x − SMA) / population sd; NaN when sd is 0."""
    deviation = stdev(x, n)
    return np.where(deviation > 0, (x - sma(x, n)) / np.where(deviation > 0, deviation, 1.0), np.nan)


def volatility(close: np.ndarray, n: int, periods_per_year: float) -> np.ndarray:
    """The sample standard deviation of the last n log returns of close, × √P."""
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.log(_ratio(close, shift(close, 1)))
    return np.sqrt(_variance(np.where(np.isfinite(returns), returns, np.nan), n, ddof=1) * periods_per_year)


def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, session: np.ndarray) -> np.ndarray:
    """Minute timelines only: the volume-weighted typical price (H + L + C) / 3 from the session's first bar to t."""
    typical = (high + low + close) / 3
    weight = np.where(np.isfinite(typical) & np.isfinite(volume), volume, 0.0)
    value = np.where(weight > 0, typical * weight, 0.0)
    out = np.full(np.shape(close), np.nan)
    for start, stop in session_blocks(session):
        weights = np.cumsum(weight[start:stop], axis=0)
        values = np.cumsum(value[start:stop], axis=0)
        out[start:stop] = np.where(weights > 0, values / np.where(weights > 0, weights, 1.0), np.nan)
    return out


def session_blocks(session: np.ndarray) -> list[tuple[int, int]]:
    """The [start, stop) rows of each run of one session index."""
    edges = np.flatnonzero(np.diff(session) != 0) + 1
    bounds = [0, *edges.tolist(), len(session)]
    return [(start, stop) for start, stop in zip(bounds[:-1], bounds[1:]) if stop > start]


def shift(x: np.ndarray, k: int) -> np.ndarray:
    """x moved k rows later; the first k rows are NaN."""
    x = np.asarray(x, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def _recursive(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """e = α·x + (1 − α)·e_prev, seeded with the mean of the first n values of each run without NaN."""
    x = np.asarray(x, dtype=np.float64)
    seed = sma(x, n)
    out = np.full(x.shape, np.nan)
    state = np.full(x.shape[1:], np.nan)
    for t in range(len(x)):
        # A NaN input makes the state NaN, and seed[t] stays NaN until n values follow it.
        state = np.where(np.isnan(state), seed[t], alpha * x[t] + (1 - alpha) * state)
        out[t] = state
    return out


def _variance(x: np.ndarray, n: int, ddof: int) -> np.ndarray:
    """Two-pass variance of each window, added in a fixed order; exactly 0 for a window of equal values."""
    x = np.asarray(x, dtype=np.float64)
    mean = sma(x, n)
    total = np.zeros(x.shape)
    for lag in range(n):
        deviation = shift(x, lag) - mean
        total += deviation * deviation
    flat = highest(x, n) == lowest(x, n)
    return np.where(flat, 0.0, total / (n - ddof))


def _extreme(x: np.ndarray, n: int, reduce) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    if n <= len(x):
        out[n - 1:] = reduce(sliding_window_view(x, n, axis=0), axis=-1)
    return out


def _ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """numerator / denominator, NaN where that is not a finite number."""
    with np.errstate(divide="ignore", invalid="ignore"):
        quotient = numerator / denominator
    return np.where(np.isfinite(quotient), quotient, np.nan)
