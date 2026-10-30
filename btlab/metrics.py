"""Metrics and tables of a run and of its benchmark (SPEC.md §12). Pure.

Points are the bars of the daily timeline or the session ends of minute timelines. An undefined value (a division by
zero, too few points) is None, which JSON carries as null.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from btlab.records import Trade

DAYS_PER_YEAR = 365.25


@dataclass(frozen=True, eq=False)
class Curve:
    """Equity points with each point's calendar day and the DTB3 rate in percent in force that day."""

    days: np.ndarray
    equity: np.ndarray
    rf: np.ndarray

    def since_first_value(self) -> "Curve":
        """The curve from its first finite point on, as a benchmark that starts late is (§10.9)."""
        known = np.flatnonzero(np.isfinite(self.equity))
        first = known[0] if len(known) else len(self.equity)
        return Curve(self.days[first:], self.equity[first:], self.rf[first:])


def risk_free(days: np.ndarray, rf_days: np.ndarray, rf_pct: np.ndarray) -> np.ndarray:
    """The latest DTB3 value on or before each day; 0 before its first value (§12)."""
    if not len(rf_days):
        return np.zeros(len(days))
    index = np.searchsorted(rf_days, days, side="right") - 1
    return np.where(index >= 0, rf_pct[np.maximum(index, 0)], 0.0)


def returns(curve: Curve) -> np.ndarray:
    """r_t = E_t / E_(t−1) − 1."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return curve.equity[1:] / curve.equity[:-1] - 1


def periods_per_year(curve: Curve) -> float | None:
    """P = number of returns / (calendar days from the first to the last point / 365.25)."""
    count = len(curve.equity) - 1
    span = _span(curve)
    return count / (span / DAYS_PER_YEAR) if count > 0 and span > 0 else None


def excess_returns(curve: Curve) -> np.ndarray | None:
    """e_t = r_t − rf_t, with rf_t = y_t / 100 / P."""
    per_year = periods_per_year(curve)
    return None if per_year is None else returns(curve) - curve.rf[1:] / 100 / per_year


def curve_metrics(curve: Curve) -> dict[str, float | int | None]:
    per_year = periods_per_year(curve)
    period_returns, excess = returns(curve), excess_returns(curve)
    first, last = (curve.equity[0], curve.equity[-1]) if len(curve.equity) else (math.nan, math.nan)
    growth = last / first if first > 0 else math.nan
    span = _span(curve)
    cagr = _value(growth ** (DAYS_PER_YEAR / span) - 1) if span > 0 and growth > 0 else None
    root = math.sqrt(per_year) if per_year else math.nan
    deep = drawdown(curve.equity)
    worst = _value(deep.max()) if len(deep) else None
    downside = math.sqrt(np.mean(np.minimum(excess, 0) ** 2)) if excess is not None and len(excess) else math.nan
    return {
        "total_return": _value(growth - 1),
        "cagr": cagr,
        "volatility": _value(_sample_sd(period_returns) * root),
        "sharpe": _value(_ratio(np.mean(excess), _sample_sd(excess)) * root) if excess is not None and len(excess) else None,
        "sortino": _value(_ratio(np.mean(excess), downside) * root) if excess is not None and len(excess) else None,
        "max_drawdown": worst,
        "max_drawdown_days": _longest_drawdown(curve) if len(curve.equity) else None,
        "calmar": _value(cagr / worst) if cagr is not None and worst else None,
    }


def trade_metrics(trades: Sequence[Trade]) -> dict[str, float | int | None]:
    """Closed trades only; OPEN trades are counted apart (§12)."""
    closed = [trade for trade in trades if trade.exit_reason != "OPEN"]
    wins = [trade.pnl for trade in closed if trade.pnl > 0]
    losses = [trade.pnl for trade in closed if trade.pnl < 0]
    return {
        "trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "win_rate": _value(len(wins) / len(closed)) if closed else None,
        "payoff_ratio": _value(np.mean(wins) / abs(np.mean(losses))) if wins and losses else None,
        "profit_factor": _value(sum(wins) / abs(sum(losses))) if losses else None,
        "expectancy": _value(np.mean([trade.pnl for trade in closed])) if closed else None,
        "expectancy_pct": _value(np.mean([trade.return_pct for trade in closed])) if closed else None,
    }


def activity_metrics(curve: Curve, gross: np.ndarray, positions: np.ndarray, traded_value: float) -> dict[str, float | None]:
    """Exposure, time in market and turnover (Σ |traded value| / 2 / mean equity, per year)."""
    years = _span(curve) / DAYS_PER_YEAR
    mean_equity = float(np.mean(curve.equity)) if len(curve.equity) else math.nan
    known = gross[np.isfinite(gross)]
    return {
        "exposure": _value(np.mean(known)) if len(known) else None,
        "time_in_market": _value(np.mean(positions > 0)) if len(positions) else None,
        "turnover": _value(traded_value / 2 / mean_equity / years) if years > 0 and mean_equity > 0 else None,
    }


def relative_metrics(strategy: Curve, benchmark: Curve) -> dict[str, float | None]:
    """β = cov(e_s, e_b) / var(e_b); α = (mean(e_s) − β·mean(e_b)) × P; the correlation of r_s and r_b. Over the
    points where both curves have a return; the benchmark's equity is NaN where it has none."""
    nothing = {"alpha": None, "beta": None, "correlation": None}
    per_year = periods_per_year(strategy)
    if per_year is None:
        return nothing
    ours, theirs = returns(strategy), returns(Curve(strategy.days, benchmark.equity, strategy.rf))
    both = np.isfinite(ours) & np.isfinite(theirs)
    if both.sum() < 2:
        return nothing
    rf = strategy.rf[1:][both] / 100 / per_year
    excess, benchmark_excess = ours[both] - rf, theirs[both] - rf
    variance = np.var(benchmark_excess, ddof=1)
    beta = np.cov(excess, benchmark_excess, ddof=1)[0, 1] / variance if variance > 0 else math.nan
    spread = np.std(ours[both]) * np.std(theirs[both])
    correlation = np.corrcoef(ours[both], theirs[both])[0, 1] if spread > 0 else math.nan
    return {
        "alpha": _value((np.mean(excess) - beta * np.mean(benchmark_excess)) * per_year),
        "beta": _value(beta),
        "correlation": _value(correlation),
    }


def drawdown(equity: np.ndarray) -> np.ndarray:
    """1 − E_t / max_(s ≤ t) E_s at each point."""
    if not len(equity):
        return np.zeros(0)
    peak = np.fmax.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1 - equity / peak


def annual_returns(curve: Curve) -> list[dict[str, float | int | None]]:
    """Compounded per calendar year; a return belongs to the year of its point."""
    days = curve.days[1:].astype("datetime64[D]")
    years = days.astype("datetime64[Y]").astype(np.int64) + 1970
    return [{"year": int(year), "return": value} for (year,), value in _compounded(returns(curve), years[:, None])]


def monthly_returns(curve: Curve) -> list[dict[str, float | int | None]]:
    """Compounded per year and month."""
    months = curve.days[1:].astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)
    keys = np.column_stack([months // 12 + 1970, months % 12 + 1])
    return [{"year": int(year), "month": int(month), "return": value} for (year, month), value in _compounded(returns(curve), keys)]


def _compounded(period_returns: np.ndarray, keys: np.ndarray) -> list[tuple[tuple[int, ...], float | None]]:
    groups = {}
    for key, value in zip(map(tuple, keys.tolist()), period_returns):
        groups.setdefault(key, []).append(value)
    return [(key, _value(np.prod(1 + np.array(values)) - 1)) for key, values in groups.items()]


def _longest_drawdown(curve: Curve) -> int:
    """The longest calendar span from a peak until equity regains it, or until the end."""
    longest, peak, peak_day, below = 0, -math.inf, curve.days[0], False
    for day, equity in zip(curve.days.tolist(), curve.equity.tolist()):
        if equity >= peak:
            if below:
                longest = max(longest, day - peak_day)
            peak, peak_day, below = equity, day, False
        else:
            below = True
    if below:
        longest = max(longest, int(curve.days[-1]) - peak_day)
    return int(longest)


def _span(curve: Curve) -> float:
    return float(curve.days[-1] - curve.days[0]) if len(curve.days) else 0.0


def _sample_sd(values: np.ndarray | None) -> float:
    return float(np.std(values, ddof=1)) if values is not None and len(values) >= 2 else math.nan


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator and math.isfinite(denominator) else math.nan


def _value(number: float) -> float | None:
    """A JSON-ready number: a Python float, or None when it is not finite."""
    number = float(number)
    return number if math.isfinite(number) else None
