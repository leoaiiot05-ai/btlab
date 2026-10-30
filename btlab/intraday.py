"""Minute timelines: resampling, the timeline, close-out and the PDT flag (SPEC.md §11). Pure."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from btlab.calendar import session
from btlab.engine import Timeline
from btlab.markets import MARKETS
from btlab.prices import Aligned
from btlab.records import MinuteBars, Trade, to_day

TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
MINUTE_S = 60
# Minute data load from this many covered sessions before the start (§10.1).
WARMUP_SESSIONS = 5
# FINRA's pattern-day-trader rule ended on 2026-06-04 (§11.6).
PDT_END = to_day(date(2026, 6, 4))
PDT_EQUITY = 25_000.0
PDT_WINDOW = 5
PDT_DAY_TRADES = 4


def resample(bars: MinuteBars, opens: np.ndarray, minutes: int) -> MinuteBars:
    """One symbol's 1-minute bars as N-minute bars in buckets aligned to their session's open (§11.1): bucket k covers
    [open + k·N, open + (k + 1)·N) and is stamped with its start. opens holds each 1-minute bar's session open."""
    if minutes == 1 or not len(bars.t):
        return bars
    width = minutes * MINUTE_S
    keys = opens + (bars.t - opens) // width * width
    starts = np.flatnonzero(np.concatenate([[True], keys[1:] != keys[:-1]]))
    ends = np.concatenate([starts[1:], [len(keys)]]) - 1
    return MinuteBars(
        keys[starts], bars.open[starts], np.maximum.reduceat(bars.high, starts), np.minimum.reduceat(bars.low, starts),
        bars.close[ends], np.add.reduceat(bars.volume, starts),
    )


@dataclass(frozen=True, eq=False)
class MinuteRun:
    """A minute timeline and what goes with it: volume is adjusted, real marks the bars a symbol has, minutes counts
    from the session's open to each bar's end, session_days and session_ends describe the sessions, and
    bars_per_session sets VOLATILITY's P (§8.1)."""

    timeline: Timeline
    volume: np.ndarray
    real: np.ndarray
    minutes: np.ndarray
    session_days: np.ndarray
    session_ends: np.ndarray
    bars_per_session: float
    slices: np.ndarray
    sources: tuple[MinuteBars, ...]
    multiplier: np.ndarray

    def paths(self, adjusted: bool) -> "TimelinePaths":
        """The overlays' minute paths: each bar's 1-minute bars, raw or adjusted (§10.8)."""
        return TimelinePaths(self.sources, self.slices, self.multiplier if adjusted else None)


class TimelinePaths:
    """The 1-minute bars inside each bar of a minute timeline (the exits' MinutePaths)."""

    def __init__(self, sources: tuple[MinuteBars, ...], slices: np.ndarray, multiplier: np.ndarray | None) -> None:
        self.sources, self.slices, self.multiplier = sources, slices, multiplier

    def bars(self, t: int, j: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        first, last = self.slices[t, j]
        if last <= first:
            return None
        source = self.sources[j]
        scale = 1.0 if self.multiplier is None else self.multiplier[t, j]
        return tuple(getattr(source, name)[first:last] * scale for name in ("open", "high", "low", "close"))


def minute_timeline(
    market: str, timeframe: str, daily: Aligned, minutes: Sequence[MinuteBars], covered: Sequence[np.ndarray],
    start_day: int, close_out: str = "NONE", close_out_minutes: int = 0,
) -> MinuteRun:
    """The bars of every session where at least one symbol has coverage OK (§11.2), from 5 covered sessions before the
    start. daily is the run's daily data, for adjustments, corporate actions and locks."""
    width = TIMEFRAME_MINUTES[timeframe] * MINUTE_S
    covered = [np.intersect1d(np.asarray(days, dtype=np.int64), daily.days) for days in covered]
    all_days = np.unique(np.concatenate(covered)) if covered else np.zeros(0, dtype=np.int64)
    first = max(0, int(np.searchsorted(all_days, start_day)) - WARMUP_SESSIONS)
    days = all_days[first:]
    bounds = np.array([session(market, int(day)) for day in days], dtype=np.int64).reshape(-1, 2)
    opens, closes = bounds[:, 0], bounds[:, 1]
    symbol_bars, sources = [], []
    for bars, days_ok in zip(minutes, covered):
        kept, owner = _in_sessions(bars, opens, closes, np.isin(days, days_ok))
        sources.append(kept)
        symbol_bars.append(resample(kept, opens[owner], TIMEFRAME_MINUTES[timeframe]))
    ends = np.array([max([s.t[np.searchsorted(s.t, close) - 1] + MINUTE_S for s in sources
                          if np.searchsorted(s.t, open_) < np.searchsorted(s.t, close)] or [close])
                     for open_, close in zip(opens, closes)], dtype=np.int64)
    times = np.unique(np.concatenate([bars.t for bars in symbol_bars])) if symbol_bars else np.zeros(0, dtype=np.int64)
    sessions = np.searchsorted(opens, times, side="right") - 1
    timeline_days = days[sessions]
    raw, real = _fill(symbol_bars, times)
    daily_index = np.searchsorted(daily.days, timeline_days)
    multiplier = daily.multiplier[daily_index]
    adjusted = {name: raw[name] * multiplier for name in ("open", "high", "low", "close")}
    # Volumes scale by the share ratios after the day only (§6.3), which the day's daily volumes show.
    with np.errstate(divide="ignore", invalid="ignore"):
        volume_scale = daily.adj_volume / daily.volume
    adjusted["volume"] = raw["volume"] * np.where(np.isfinite(volume_scale) & (volume_scale > 0), volume_scale, 1.0)[daily_index]
    _carry(raw, real)
    _carry(adjusted, real)
    cash, ratio = _actions(daily, days, sessions, times.shape[0])
    tradable = real & np.isfinite(raw["close"]) & np.isfinite(raw["open"]) & (raw["volume"] > 0)
    bar_ends = np.minimum(times + width, ends[sessions])
    first_bar = int(np.searchsorted(timeline_days, start_day))
    timeline = Timeline(
        symbols=daily.symbols, time=times, day=timeline_days, session=sessions,
        open=raw["open"], high=raw["high"], low=raw["low"], close=raw["close"],
        adj_open=adjusted["open"], adj_high=adjusted["high"], adj_low=adjusted["low"], adj_close=adjusted["close"],
        tradable=tradable, lock=daily.lock[daily_index], cash=cash, ratio=ratio, start=first_bar,
        closeout=_closeout(sessions, bar_ends, ends, close_out_minutes) if close_out == "SESSION_END" else None,
    )
    spec = MARKETS[market].session
    regular = (spec.close.hour * 60 + spec.close.minute) - (spec.open.hour * 60 + spec.open.minute)
    per_session = math.ceil(regular / TIMEFRAME_MINUTES[timeframe])
    slices = np.stack([np.column_stack([np.searchsorted(s.t, times), np.searchsorted(s.t, bar_ends)]) for s in sources],
                      axis=1) if sources else np.zeros((len(times), 0, 2), dtype=np.int64)
    return MinuteRun(
        timeline=timeline, volume=adjusted["volume"], real=real, minutes=(bar_ends - opens[sessions]) / MINUTE_S,
        session_days=days, session_ends=ends,
        bars_per_session=per_session, slices=slices, sources=tuple(sources), multiplier=multiplier,
    )


def _in_sessions(bars: MinuteBars, opens: np.ndarray, closes: np.ndarray, covered: np.ndarray) -> tuple[MinuteBars, np.ndarray]:
    """The bars inside the symbol's covered sessions, and the session of each."""
    owner = np.searchsorted(opens, bars.t, side="right") - 1
    inside = (owner >= 0) & (bars.t < closes[np.maximum(owner, 0)]) & covered[np.maximum(owner, 0)]
    return MinuteBars(*(getattr(bars, name)[inside] for name in ("t", "open", "high", "low", "close", "volume"))), owner[inside]


def _fill(symbol_bars: list[MinuteBars], times: np.ndarray) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Bars × symbols arrays of the symbols' own bars; NaN elsewhere."""
    shape = (len(times), len(symbol_bars))
    raw = {name: np.full(shape, np.nan) for name in ("open", "high", "low", "close", "volume")}
    real = np.zeros(shape, dtype=bool)
    for j, bars in enumerate(symbol_bars):
        rows = np.searchsorted(times, bars.t)
        real[rows, j] = True
        for name in raw:
            raw[name][rows, j] = getattr(bars, name)
    return raw, real


def _carry(prices: dict[str, np.ndarray], real: np.ndarray) -> None:
    """At a time without its own bar, a symbol carries its last close into open, high, low and close, with volume 0;
    before its first bar it stays NaN (§11.2)."""
    rows = np.arange(real.shape[0])[:, None]
    source = np.maximum.accumulate(np.where(real, rows, -1), axis=0)
    carried = np.take_along_axis(prices["close"], np.maximum(source, 0), axis=0)
    carried[source < 0] = np.nan
    for name in ("open", "high", "low", "close"):
        prices[name] = np.where(real, prices[name], carried)
    prices["volume"] = np.where(real, prices["volume"], np.where(source >= 0, 0.0, np.nan))


def _actions(daily: Aligned, days: np.ndarray, sessions: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """The corporate actions of each day on the first bar of its session, or of the next session (§11.5)."""
    cash, ratio = np.zeros((count, len(daily.symbols))), np.ones((count, len(daily.symbols)))
    first_bars = np.searchsorted(sessions, np.arange(len(days)))
    for k in np.flatnonzero(((daily.ratio != 1) | (daily.cash != 0)).any(axis=1)):
        target = int(np.searchsorted(days, daily.days[k]))
        if len(days) and daily.days[k] >= days[0] and target < len(days):
            cash[first_bars[target]] += daily.cash[k]
            ratio[first_bars[target]] *= daily.ratio[k]
    return cash, ratio


def _closeout(sessions: np.ndarray, bar_ends: np.ndarray, session_ends: np.ndarray, minutes: int) -> np.ndarray:
    """1 on each session's close-out bar, the last that ends at least m minutes before the session's end; 2 on the
    session's later bars (§11.4). A session without such a bar is closed out from its first bar."""
    marks = np.zeros(len(sessions), dtype=np.int8)
    eligible = bar_ends <= session_ends[sessions] - minutes * MINUTE_S
    for index in np.unique(sessions):
        bars = np.flatnonzero(sessions == index)
        candidates = bars[eligible[bars]]
        if len(candidates):
            marks[candidates[-1]] = 1
            marks[bars[bars > candidates[-1]]] = 2
        else:
            marks[bars] = 2
    return marks


def session_end_bars(sessions: np.ndarray, start: int) -> np.ndarray:
    """The last bar of each session from the first trading bar on: the points of the metrics (§11.7)."""
    last = np.flatnonzero(np.concatenate([sessions[1:] != sessions[:-1], [True]]))
    return last[last >= start]


def pdt_windows(trades: Sequence[Trade], times: np.ndarray, sessions: np.ndarray, session_days: np.ndarray,
                session_equity: np.ndarray, start_session: int) -> int:
    """§11.6, informational and US only: windows of 5 consecutive sessions with 4 or more day trades (opened and closed
    in one session) on sessions before 2026-06-04, while the run's session-end equity is below USD 25,000.
    session_equity holds the session-end equity of the sessions from start_session on."""
    count = len(session_days)
    day_trades = np.zeros(count, dtype=np.int64)
    for trade in trades:
        if trade.exit_reason == "OPEN":
            continue
        entry, exit_ = (sessions[np.searchsorted(times, moment, side="right") - 1] for moment in (trade.entry_time, trade.exit_time))
        if entry == exit_:
            day_trades[entry] += 1
    day_trades[session_days >= PDT_END] = 0
    equity = np.full(count, np.inf)
    equity[start_session: start_session + len(session_equity)] = session_equity
    return sum(
        1 for first in range(start_session, count - PDT_WINDOW + 1)
        if session_days[first] < PDT_END and day_trades[first: first + PDT_WINDOW].sum() >= PDT_DAY_TRADES
        and equity[first: first + PDT_WINDOW].min() < PDT_EQUITY
    )
