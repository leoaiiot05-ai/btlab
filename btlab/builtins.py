"""The built-in strategies A to J (SPEC.md §9.3). Pure.

Each reads the run's adjusted bars (§10.2) and returns DIRECTION or WEIGHTS targets, or, for DCA, an event-driven
strategy that orders through the engine's Book. NaN is no instruction (§9.1).
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from btlab import indicators
from btlab.calendar import period_starts
from btlab.engine import Book, OrderStrategy, Targets
from btlab.indicators import Inputs
from btlab.params import Param

# DUAL_MOMENTUM compounds the risk-free rate per bar with the daily timeline's bars per year (§8.1).
BARS_PER_YEAR = 252
VOLATILITY_BARS = 60
DCA_AMOUNT = {"US": 1_000.0, "TW": 30_000.0}


@dataclass(frozen=True, eq=False)
class View:
    """What a built-in reads: the run's adjusted bars from the warm-up on, the DTB3 rate in percent per bar, and the
    first trading bar."""

    symbols: tuple[str, ...]
    days: np.ndarray
    bars: Inputs
    rf: np.ndarray
    start: int


Schema = dict[str, Param]


@dataclass(frozen=True)
class Builtin:
    """A built-in strategy. schema takes the market and the run's symbols; check returns the errors of parameters
    that depend on each other, as (name, message); run returns the targets."""

    id: str
    name_en: str
    name_zh: str
    output: str
    schema: Callable[[str, tuple[str, ...]], Schema]
    run: Callable[[View, dict], "Targets | OrderStrategy"]
    check: Callable[[dict], list[tuple[str, str]]] = field(default=lambda params: [])


# ----- schemas -----


def _int(default: int, low: int, high: int, en: str, zh: str) -> Param:
    return Param("int", default, low, high, 1, label_en=en, label_zh=zh)


def _float(default: float, low: float, high: float, step: float, en: str, zh: str) -> Param:
    return Param("float", default, low, high, step, label_en=en, label_zh=zh)


SHORTS = Param("bool", False, label_en="Allow shorts", label_zh="允許放空")


def _fast_below_slow(params: dict) -> list[tuple[str, str]]:
    return [] if params["fast"] < params["slow"] else [("fast", "must be below slow")]


# ----- helpers -----


def _states(known: np.ndarray, enter_long: np.ndarray, exit_long: np.ndarray, enter_short: np.ndarray,
            exit_short: np.ndarray, allow_short: bool) -> np.ndarray:
    """One state machine per symbol: flat to long or short on an entry, back to flat on its exit. An exit and an
    entry of the other side on the same bar reverse (§9.3). NaN where the rule's indicators are not known."""
    state = np.zeros(known.shape[1])
    out = np.full(known.shape, np.nan)
    for t in range(len(known)):
        state = np.where(((state > 0) & exit_long[t]) | ((state < 0) & exit_short[t]), 0.0, state)
        flat = state == 0
        state = np.where(flat & enter_long[t], 1.0, state)
        if allow_short:
            state = np.where(flat & ~enter_long[t] & enter_short[t], -1.0, state)
        out[t] = np.where(known[t], state, np.nan)
    return out


def _above(fast: np.ndarray, slow: np.ndarray, allow_short: bool) -> Targets:
    """+1 while fast is above slow; otherwise −1 when shorts are allowed, else 0."""
    direction = np.where(fast > slow, 1.0, -1.0 if allow_short else 0.0)
    return Targets("DIRECTION", np.where(np.isnan(fast) | np.isnan(slow), np.nan, direction))


def _ranked(scores: np.ndarray, count: int) -> np.ndarray:
    """The indices of the count highest finite scores, ties to the earlier symbol."""
    candidates = np.flatnonzero(np.isfinite(scores))
    order = sorted(candidates.tolist(), key=lambda j: (-scores[j], j))
    return np.array(order[:count], dtype=np.int64)


# ----- A to J -----


def ma_cross(view: View, p: dict) -> Targets:
    average = indicators.sma if p["ma"] == "SMA" else indicators.ema
    close = view.bars.close
    return _above(average(close, p["fast"]), average(close, p["slow"]), p["allow_short"])


def rsi(view: View, p: dict) -> Targets:
    value = indicators.rsi(view.bars.close, p["n"])
    low, high = value < p["lower"], value > p["upper"]
    return Targets("DIRECTION", _states(np.isfinite(value), low, high, high, low, p["allow_short"]))


def macd(view: View, p: dict) -> Targets:
    lines = indicators.macd(view.bars.close, p["fast"], p["slow"], p["signal"])
    return _above(lines["macd"], lines["signal"], p["allow_short"])


def bollinger(view: View, p: dict) -> Targets:
    close = view.bars.close
    bands = indicators.bbands(close, p["n"], p["k"])
    known = np.isfinite(bands["upper"]) & np.isfinite(close)
    return Targets("DIRECTION", _states(known, close < bands["lower"], close > bands["middle"], close > bands["upper"],
                                        close < bands["middle"], p["allow_short"]))


def breakout(view: View, p: dict) -> Targets:
    close = view.bars.close
    entry = indicators.donchian(view.bars.high, view.bars.low, p["entry_n"])
    exit_ = indicators.donchian(view.bars.high, view.bars.low, p["exit_n"])
    known = np.isfinite(entry["upper"]) & np.isfinite(exit_["upper"]) & np.isfinite(close)
    return Targets("DIRECTION", _states(known, close > entry["upper"], close < exit_["lower"], close < entry["lower"],
                                        close > exit_["upper"], p["allow_short"]))


def buy_hold(view: View, p: dict) -> Targets:
    """On the run's first bar, equal weights over the symbols that have data; then hold."""
    weights = np.full(view.bars.close.shape, np.nan)
    have = np.isfinite(view.bars.close[view.start])
    if have.any():
        weights[view.start] = np.where(have, 1 / have.sum(), 0.0)
    return Targets("WEIGHTS", weights)


class _Dca:
    """On the first bar of each period, amount / N for each of the N symbols with data, while the cash covers each
    buy with its costs. Never sells."""

    def __init__(self, view: View, amount: float, frequency: str) -> None:
        self.starts = period_starts(view.days, frequency)
        self.amount = amount

    def on_bar(self, book: Book) -> None:
        if not self.starts[book.t]:
            return
        have = [j for j in range(len(book.positions)) if np.isfinite(book.price(j))]
        cash = book.cash
        for j in have:
            each = self.amount / len(have)
            need = book.buy_cost(j, each)
            if need > cash:
                break
            book.order_value(j, each)
            cash -= need


def dca(view: View, p: dict) -> OrderStrategy:
    return _Dca(view, p["amount"], p["frequency"])


def momentum_rotation(view: View, p: dict) -> Targets:
    """At each period start: the top_k by ROC(lookback) at t − skip (above 0 with abs_filter); equal weights, or
    weights ∝ 1/VOLATILITY(60) scaled to selected/top_k. Long only."""
    close = view.bars.close
    starts = period_starts(view.days, p["rebalance"])
    score = indicators.shift(indicators.roc(close, p["lookback"]), p["skip"])
    if p["abs_filter"]:
        score = np.where(score > 0, score, np.nan)
    volatility = indicators.volatility(close, VOLATILITY_BARS, BARS_PER_YEAR)
    weights = np.full(close.shape, np.nan)
    top_k = p["top_k"]
    for t in np.flatnonzero(starts):
        chosen = _ranked(score[t], top_k)
        row = np.zeros(close.shape[1])
        if p["weighting"] == "equal":
            row[chosen] = 1 / top_k
        else:
            usable = chosen[np.isfinite(volatility[t, chosen]) & (volatility[t, chosen] > 0)]
            inverse = 1 / volatility[t, usable]
            row[usable] = inverse / inverse.sum() * len(usable) / top_k if len(usable) else 0.0
        weights[t] = row
    return Targets("WEIGHTS", weights, starts)


def dual_momentum(view: View, p: dict) -> Targets:
    """On the first bar of each month: 100% in the risk symbol with the highest ROC(lookback) when that ROC is above 0
    and above the compounded risk-free return over the same bars; otherwise 100% in safe, or cash. Long only."""
    close = view.bars.close
    starts = period_starts(view.days, p["rebalance"])
    score = indicators.roc(close, p["lookback"])
    growth = np.log1p(view.rf / 100 / BARS_PER_YEAR)
    total = np.concatenate([[0.0], np.cumsum(growth)])
    lookback = p["lookback"]
    risk_free = np.full(len(growth), np.nan)
    risk_free[lookback:] = np.expm1(total[lookback + 1:] - total[1:-lookback])
    safe = view.symbols.index(p["safe"]) if p["safe"] else None
    risk = np.array([j for j in range(close.shape[1]) if j != safe], dtype=np.int64)
    weights = np.full(close.shape, np.nan)
    for t in np.flatnonzero(starts):
        row = np.zeros(close.shape[1])
        best = _ranked(score[t, risk], 1)
        if len(best) and score[t, risk[best[0]]] > max(0.0, risk_free[t]):
            row[risk[best[0]]] = 1.0
        elif safe is not None:
            row[safe] = 1.0
        weights[t] = row
    return Targets("WEIGHTS", weights, starts)


def zscore_reversion(view: View, p: dict) -> Targets:
    z = indicators.zscore(view.bars.close, p["n"])
    return Targets("DIRECTION", _states(np.isfinite(z), z < -p["entry_z"], z > -p["exit_z"], z > p["entry_z"],
                                        z < p["exit_z"], p["allow_short"]))


# ----- the registry -----


def _period(default: str, *choices: str) -> Param:
    return Param("choice", default, choices=choices, label_en="Rebalance", label_zh="再平衡週期")


BUILTINS = {
    builtin.id: builtin
    for builtin in (
        Builtin("MA_CROSS", "Moving-average cross", "均線交叉", "DIRECTION", lambda market, symbols: {
            "fast": _int(20, 2, 200, "Fast", "快線"),
            "slow": _int(50, 5, 400, "Slow", "慢線"),
            "ma": Param("choice", "SMA", choices=("SMA", "EMA"), label_en="Average", label_zh="均線"),
            "allow_short": SHORTS,
        }, ma_cross, _fast_below_slow),
        Builtin("RSI", "RSI", "相對強弱指標", "DIRECTION", lambda market, symbols: {
            "n": _int(14, 2, 100, "Length", "週期"),
            "lower": _float(30.0, 1, 50, 1, "Lower", "下限"),
            "upper": _float(70.0, 50, 99, 1, "Upper", "上限"),
            "allow_short": SHORTS,
        }, rsi),
        Builtin("MACD", "MACD", "MACD", "DIRECTION", lambda market, symbols: {
            "fast": _int(12, 2, 100, "Fast", "快線"),
            "slow": _int(26, 3, 200, "Slow", "慢線"),
            "signal": _int(9, 2, 100, "Signal", "訊號線"),
            "allow_short": SHORTS,
        }, macd, _fast_below_slow),
        Builtin("BOLLINGER", "Bollinger bands", "布林通道", "DIRECTION", lambda market, symbols: {
            "n": _int(20, 5, 200, "Length", "週期"),
            "k": _float(2.0, 0.5, 4.0, 0.1, "Width (k)", "寬度 (k)"),
            "allow_short": SHORTS,
        }, bollinger),
        Builtin("BREAKOUT", "Channel breakout", "通道突破", "DIRECTION", lambda market, symbols: {
            "entry_n": _int(20, 5, 250, "Entry channel", "進場通道"),
            "exit_n": _int(10, 2, 250, "Exit channel", "出場通道"),
            "allow_short": SHORTS,
        }, breakout),
        Builtin("BUY_HOLD", "Buy and hold", "買進持有", "WEIGHTS", lambda market, symbols: {}, buy_hold),
        Builtin("DCA", "Dollar-cost averaging", "定期定額", "ORDERS", lambda market, symbols: {
            "amount": Param("float", DCA_AMOUNT[market], 1.0, None, 100.0, label_en="Amount", label_zh="每期金額"),
            "frequency": Param("choice", "monthly", choices=("monthly", "weekly"), label_en="Frequency", label_zh="頻率"),
        }, dca),
        Builtin("MOMENTUM_ROTATION", "Momentum rotation", "動能輪動", "WEIGHTS", lambda market, symbols: {
            "lookback": _int(126, 20, 504, "Lookback", "回顧期"),
            "skip": _int(0, 0, 63, "Skip", "略過"),
            "top_k": _int(3, 1, 20, "Top K", "前 K 名"),
            "rebalance": _period("monthly", "monthly", "weekly"),
            "weighting": Param("choice", "equal", choices=("equal", "vol_inverse"), label_en="Weighting", label_zh="權重"),
            "abs_filter": Param("bool", True, label_en="Positive momentum only", label_zh="僅正動能"),
        }, momentum_rotation),
        Builtin("DUAL_MOMENTUM", "Dual momentum", "雙動能", "WEIGHTS", lambda market, symbols: {
            "lookback": _int(252, 20, 504, "Lookback", "回顧期"),
            "safe": Param("choice", "", choices=("", *symbols), label_en="Safe asset", label_zh="避險資產"),
            "rebalance": _period("monthly", "monthly"),
        }, dual_momentum),
        Builtin("ZSCORE_REVERSION", "Z-score reversion", "Z 分數回歸", "DIRECTION", lambda market, symbols: {
            "n": _int(20, 5, 200, "Length", "週期"),
            "entry_z": _float(2.0, 0.5, 4.0, 0.1, "Entry z", "進場 z"),
            "exit_z": _float(0.5, 0.0, 3.0, 0.1, "Exit z", "出場 z"),
            "allow_short": SHORTS,
        }, zscore_reversion),
    )
}
