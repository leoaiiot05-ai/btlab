"""Exit overlays and intrabar paths (SPEC.md §10.8). Pure.

Overlays apply to every position whatever the strategy. Levels are for longs; shorts are mirrored.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from btlab.engine import OpenTrade, Simulation
from btlab.features import SessionBars
from btlab.indicators import atr as average_true_range

# On a tie, stops win over profit exits; within each group, this order decides (§10.8).
STOPS = ("SL", "TRAIL", "ATR", "IBG_SL")
PROFITS = ("TP", "IBG_TP")


@dataclass(frozen=True)
class ExitRules:
    """The overlays of a run (§10.1 `exits`); None turns one off. Percentages as in the run configuration."""

    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_pct: float | None = None
    atr_n: int | None = None
    atr_mult: float | None = None
    max_hold_bars: int | None = None
    ibguard_tp_pct: float | None = None
    ibguard_sl_pct: float | None = None

    @property
    def intrabar(self) -> bool:
        """Whether any overlay triggers along a bar's path (all but MAX_HOLD)."""
        return any(value is not None for value in (
            self.stop_loss_pct, self.take_profit_pct, self.trailing_pct, self.atr_n, self.ibguard_tp_pct,
        ))


def ibguard_step(side: int, ref: float, prev: float | None, point: float, tp: float, sl: float) -> tuple[float, float | None, str | None]:
    """One tick of ibguard's rule (ibguard SPEC §4.2): ratchet, then TAKE_PROFIT or STOP_LOSS (inclusive), then
    prev = point. tp and sl are fractions; a point that is not finite or ≤ 0 is skipped. Returns (ref, prev, result)."""
    if not (math.isfinite(point) and point > 0):
        return ref, prev, None
    ref = _ratchet(side, ref, prev, point)
    if side > 0:
        result = "TAKE_PROFIT" if point >= ref * (1 + tp) else "STOP_LOSS" if point <= ref * (1 - sl) else None
    else:
        result = "TAKE_PROFIT" if point <= ref * (1 - tp) else "STOP_LOSS" if point >= ref * (1 + sl) else None
    return ref, point, result


def _ratchet(side: int, ref: float, prev: float | None, point: float) -> float:
    """Long: when the price turns down from above ref, ref rises to the previous tick. Short mirrored."""
    if prev is None:
        return ref
    if side > 0 and point < prev and prev > ref:
        return prev
    if side < 0 and point > prev and prev < ref:
        return prev
    return ref


@dataclass(frozen=True, eq=False)
class Path:
    """The prices a bar passes through. gaps[i] says the price jumped to point i; elsewhere it moves continuously."""

    points: np.ndarray
    gaps: np.ndarray


def daily_path(open_: float, high: float, low: float, close: float) -> Path:
    """O → L → H → C when the bar closes at or above its open, else O → H → L → C."""
    points = [open_, low, high, close] if close >= open_ else [open_, high, low, close]
    return Path(np.array(points, dtype=np.float64), np.array([True, False, False, False]))


def minute_path(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> Path:
    """Each 1-minute bar's O, then L and H in the order of the daily rule, then C. A minute's open that differs from
    the previous close is a gap, and so is the first open."""
    up = closes >= opens
    points = np.column_stack([opens, np.where(up, lows, highs), np.where(up, highs, lows), closes]).ravel()
    gaps = np.zeros((len(opens), 4), dtype=bool)
    gaps[:, 0] = np.concatenate([[True], opens[1:] != closes[:-1]])
    return Path(points.astype(np.float64), gaps.ravel())


@dataclass
class Watch:
    """The moving parts of a position's levels, carried from bar to bar: the trailing extreme (peak or trough) and
    ibguard's ref and prev."""

    extreme: float
    ref: float
    prev: float | None


@dataclass(frozen=True)
class Exit:
    reason: str
    price: float


def walk(path: Path, side: int, entry: float, atr_distance: float | None, rules: ExitRules, watch: Watch) -> Exit | None:
    """The first exit along a path, updating watch. At each point the trailing extreme and the ibguard ratchet move
    first, then the levels are checked: a level crossed inside a continuous segment fills at the level, one already
    crossed at a gap fills at the gap price. The level reached first wins; a tie goes to stops."""
    for index, point in enumerate(path.points):
        if not (math.isfinite(point) and point > 0):
            continue
        start = watch.prev
        watch.extreme = max(watch.extreme, point) if side > 0 else min(watch.extreme, point)
        watch.ref = _ratchet(side, watch.ref, watch.prev, point)
        watch.prev = point
        levels = _levels(side, entry, atr_distance, rules, watch)
        hit = _at_gap(side, point, levels) if path.gaps[index] or start is None else _in_segment(side, start, point, levels)
        if hit is not None:
            return hit
    return None


def _levels(side: int, entry: float, atr_distance: float | None, rules: ExitRules, watch: Watch) -> list[tuple[str, float]]:
    """(reason, level) of every active overlay, stops first, each group in its tie order."""
    levels = []
    if rules.stop_loss_pct is not None:
        levels.append(("SL", entry * (1 - side * rules.stop_loss_pct / 100)))
    if rules.trailing_pct is not None:
        levels.append(("TRAIL", watch.extreme * (1 - side * rules.trailing_pct / 100)))
    if atr_distance is not None:
        levels.append(("ATR", entry - side * atr_distance))
    if rules.ibguard_sl_pct is not None:
        levels.append(("IBG_SL", watch.ref * (1 - side * rules.ibguard_sl_pct / 100)))
    if rules.take_profit_pct is not None:
        levels.append(("TP", entry * (1 + side * rules.take_profit_pct / 100)))
    if rules.ibguard_tp_pct is not None:
        levels.append(("IBG_TP", watch.ref * (1 + side * rules.ibguard_tp_pct / 100)))
    return levels


def _at_gap(side: int, point: float, levels: list[tuple[str, float]]) -> Exit | None:
    """At a gap every level already crossed fills at the gap price; stops first."""
    for reason, level in levels:
        crossed = side * (point - level) <= 0 if reason in STOPS else side * (point - level) >= 0
        if crossed:
            return Exit(reason, point)
    return None


def _in_segment(side: int, start: float, end: float, levels: list[tuple[str, float]]) -> Exit | None:
    """Inside a continuous move from start to end: stops cross on an adverse move, profit exits on a favourable one,
    at their level; the level nearest start is reached first, and the list order breaks ties."""
    move = np.sign(end - start)
    if move == 0:
        return None
    low, high = min(start, end), max(start, end)
    crossed = [
        (abs(level - start), order, reason, level)
        for order, (reason, level) in enumerate(levels)
        if low <= level <= high and move == (-side if reason in STOPS else side)
    ]
    if not crossed:
        return None
    _, _, reason, level = min(crossed)
    return Exit(reason, level)


class MinutePaths(Protocol):
    """1-minute bars that form a bar's path, in the run's price mode, or None without minute coverage OK."""

    def bars(self, t: int, j: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None: ...


class DailyMinutePaths:
    """The daily timeline's minute paths, from each symbol's SessionBars (features.py): scaled by the day's
    multiplier in ADJUSTED mode, raw (multiplier 1) in REALISTIC mode."""

    def __init__(self, sessions: Sequence[SessionBars]) -> None:
        self.sessions = sessions

    def bars(self, t: int, j: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        s = self.sessions[j]
        start, stop = s.start[t], s.stop[t]
        if stop <= start:
            return None
        return s.open[start:stop], s.high[start:stop], s.low[start:stop], s.close[start:stop]


class Overlays:
    """The engine's exits hook for one run. minutes is None for intrabar_path DAILY; with MINUTE it gives the paths,
    and a position-day without them falls back to the daily path and counts toward PATH_FALLBACK."""

    def __init__(self, rules: ExitRules, minutes: MinutePaths | None = None) -> None:
        self.rules = rules
        self.minutes = minutes
        self.fallbacks = 0
        self._atr: np.ndarray | None = None

    def intrabar(self, simulation: Simulation, t: int) -> None:
        """Step 3 of a bar: every open position on a tradable bar, including those opened at this open."""
        if not self.rules.intrabar:
            return
        for j in sorted(simulation.open_trades):
            if not simulation.timeline.tradable[t, j]:
                continue
            trade = simulation.open_trades[j]
            self._arm(simulation, trade, j)
            watch = Watch(trade.extreme, trade.ref, trade.prev)
            hit = walk(self._path(simulation, t, j), trade.side, trade.average, trade.atr_distance, self.rules, watch)
            trade.extreme, trade.ref, trade.prev = watch.extreme, watch.ref, watch.prev
            if hit is not None:
                simulation.exit_position(t, j, hit.price, hit.reason)

    def at_close(self, simulation: Simulation, t: int) -> None:
        """MAX_HOLD: the entry bar counts as bar 1; at the close of bar N the exit becomes an order."""
        bars = self.rules.max_hold_bars
        if bars is None:
            return
        for j, trade in sorted(simulation.open_trades.items()):
            if t - trade.entry_bar + 1 >= bars:
                simulation.order(j, 0.0, "MAX_HOLD")

    def _arm(self, simulation: Simulation, trade: OpenTrade, j: int) -> None:
        """On a new position: the trailing extreme and ibguard's ref and prev start at the entry price; the ATR stop's
        distance is mult × ATR(n) at the signal bar, in the raw scale in REALISTIC mode."""
        if trade.extreme is not None:
            return
        trade.extreme = trade.ref = trade.prev = trade.average
        if self.rules.atr_n is not None:
            timeline = simulation.timeline
            if self._atr is None:
                self._atr = average_true_range(timeline.adj_high, timeline.adj_low, timeline.adj_close, self.rules.atr_n)
            signal = trade.signal_bar
            atr = self._atr[signal, j]
            if simulation.realistic:
                atr *= timeline.close[signal, j] / timeline.adj_close[signal, j]
            trade.atr_distance = float(self.rules.atr_mult * atr) if math.isfinite(atr) else None

    def _path(self, simulation: Simulation, t: int, j: int) -> Path:
        if self.minutes is not None:
            bars = self.minutes.bars(t, j)
            if bars is not None:
                return minute_path(*bars)
            self.fallbacks += 1
            simulation.flags["PATH_FALLBACK"] = self.fallbacks
        return daily_path(simulation.open[t, j], simulation.high[t, j], simulation.low[t, j], simulation.close[t, j])
