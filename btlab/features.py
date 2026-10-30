"""Minute-derived daily features, today-so-far values and daily values on minute timelines (SPEC.md §8.2–§8.4). Pure."""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from btlab.calendar import session, session_end
from btlab.indicators import Inputs, session_blocks, vwap
from btlab.records import MinuteBars

MINUTE_S = 60


@dataclass(frozen=True, eq=False)
class SessionBars:
    """One symbol's 1-minute bars cut into the sessions of a daily timeline (§8.2), prices scaled by each day's
    multiplier so they match the adjusted daily series. Day i holds the bars [start[i], stop[i]), none without coverage
    OK. open_time and end_time bound each session in UTC epoch seconds; the end is that of the last stored bar (§4.2)."""

    t: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    start: np.ndarray
    stop: np.ndarray
    open_time: np.ndarray
    end_time: np.ndarray


def session_bars(
    market: str, days: np.ndarray, bars: MinuteBars, covered: np.ndarray, multiplier: np.ndarray
) -> SessionBars:
    """covered: the days whose coverage is OK. multiplier: the adjusted ÷ raw scale of each day (Aligned.multiplier)."""
    count = len(days)
    start, stop = np.zeros(count, dtype=np.int64), np.zeros(count, dtype=np.int64)
    open_time, end_time = np.zeros(count, dtype=np.int64), np.zeros(count, dtype=np.int64)
    scale = np.ones(len(bars.t))
    covered = set(np.asarray(covered).tolist())
    for i, day in enumerate(np.asarray(days).tolist()):
        if day not in covered:
            continue
        opens, closes = session(market, day)
        first, last = np.searchsorted(bars.t, [opens, closes], side="left")
        start[i], stop[i], open_time[i] = first, last, opens
        end_time[i] = session_end(market, day, int(bars.t[last - 1]) if last > first else None)
        scale[first:last] = multiplier[i]
    return SessionBars(
        bars.t, bars.open * scale, bars.high * scale, bars.low * scale, bars.close * scale, bars.volume,
        start, stop, open_time, end_time,
    )


def last_n_min_return(s: SessionBars, n: int) -> np.ndarray:
    """Session close ÷ the close of the bar that ends n minutes before the session end − 1."""
    return _per_day(s, lambda a, b, i: s.close[b - 1] / _close_as_of(s, a, b, s.end_time[i] - n * MINUTE_S) - 1)


def first_n_min_return(s: SessionBars, n: int) -> np.ndarray:
    """The close of the bar that ends n minutes after the open ÷ the session open − 1."""
    return _per_day(s, lambda a, b, i: _close_as_of(s, a, b, s.open_time[i] + n * MINUTE_S) / s.open[a] - 1)


def close_vs_vwap(s: SessionBars) -> np.ndarray:
    return _per_day(s, lambda a, b, i: s.close[b - 1] / _session_vwap(s, a, b) - 1)


def range_position(s: SessionBars) -> np.ndarray:
    """(close − low) / (high − low) of the session; 0.5 when high = low."""
    def position(a: int, b: int, i: int) -> float:
        high, low = s.high[a:b].max(), s.low[a:b].min()
        return 0.5 if high == low else (s.close[b - 1] - low) / (high - low)

    return _per_day(s, position)


def reclaim_prev_low(s: SessionBars) -> np.ndarray:
    """1 when the session's low is below the previous session's low and its close above it, else 0."""
    lows = _per_day(s, lambda a, b, i: s.low[a:b].min())
    closes = _per_day(s, lambda a, b, i: s.close[b - 1])
    out = np.full(len(lows), np.nan)
    previous = lows[:-1]
    known = np.isfinite(lows[1:]) & np.isfinite(previous)
    out[1:] = np.where(known, ((lows[1:] < previous) & (closes[1:] > previous)).astype(float), np.nan)
    return out


def today_open(inputs: Inputs) -> np.ndarray:
    """The open of the session's first bar, from that bar on (§8.3)."""
    out = np.full(inputs.close.shape, np.nan)
    for start, stop in session_blocks(inputs.session):
        real = inputs.real[start:stop]
        first = np.argmax(real, axis=0)
        opened = np.arange(stop - start)[:, None] >= first
        value = inputs.open[start:stop][first, np.arange(real.shape[1])]
        out[start:stop] = np.where(opened & real.any(axis=0), value, np.nan)
    return out


def today_high(inputs: Inputs) -> np.ndarray:
    return _running(inputs, inputs.high, np.fmax)


def today_low(inputs: Inputs) -> np.ndarray:
    return _running(inputs, inputs.low, np.fmin)


def today_vwap(inputs: Inputs) -> np.ndarray:
    real = inputs.real
    return vwap(
        np.where(real, inputs.high, np.nan), np.where(real, inputs.low, np.nan), np.where(real, inputs.close, np.nan),
        np.where(real, inputs.volume, 0.0), inputs.session,
    )


def today_return(inputs: Inputs) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        value = inputs.close / today_open(inputs) - 1
    return np.where(np.isfinite(value), value, np.nan)


def minutes_since_open(inputs: Inputs) -> np.ndarray:
    """Minutes from the session's open to each bar's end, when the strategy evaluates the bar."""
    return np.repeat(np.asarray(inputs.minutes, dtype=np.float64)[:, None], inputs.close.shape[1], axis=1)


def previous_session(daily_days: np.ndarray, values: np.ndarray, session_days: np.ndarray) -> np.ndarray:
    """§8.4: on a minute timeline, each bar takes the daily value of the last session before its own, because a day's
    bar is not known until its close (intentional)."""
    before = np.searchsorted(daily_days, session_days, side="left") - 1
    out = values[np.maximum(before, 0)].astype(np.float64)
    out[before < 0] = np.nan
    return out


def _per_day(s: SessionBars, value: Callable[[int, int, int], float]) -> np.ndarray:
    out = np.full(len(s.start), np.nan)
    for i in np.flatnonzero(s.stop > s.start):
        with np.errstate(divide="ignore", invalid="ignore"):
            out[i] = value(int(s.start[i]), int(s.stop[i]), int(i))
    return np.where(np.isfinite(out), out, np.nan)


def _close_as_of(s: SessionBars, a: int, b: int, moment: int) -> float:
    """The close of the last bar that ends at or before moment, so a missing minute takes the latest earlier one."""
    index = np.searchsorted(s.t[a:b] + MINUTE_S, moment, side="right") - 1
    return s.close[a + index] if index >= 0 else np.nan


def _session_vwap(s: SessionBars, a: int, b: int) -> float:
    volume = s.volume[a:b]
    typical = (s.high[a:b] + s.low[a:b] + s.close[a:b]) / 3
    total = volume.sum()
    return (typical * volume).sum() / total if total > 0 else np.nan


def _running(inputs: Inputs, values: np.ndarray, accumulate: np.ufunc) -> np.ndarray:
    """A running extreme over the session's real bars; NaN before the symbol's first bar of the session."""
    out = np.full(values.shape, np.nan)
    masked = np.where(inputs.real, values, np.nan)
    for start, stop in session_blocks(inputs.session):
        out[start:stop] = accumulate.accumulate(masked[start:stop], axis=0)
    return out
