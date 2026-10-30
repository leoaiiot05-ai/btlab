"""Metrics and tables against hand-computed values (SPEC.md §12, AC-11)."""

import math
import statistics
from datetime import date

import numpy as np
import pytest

from btlab.metrics import (
    Curve,
    activity_metrics,
    annual_returns,
    curve_metrics,
    drawdown,
    monthly_returns,
    periods_per_year,
    relative_metrics,
    risk_free,
    trade_metrics,
)
from btlab.records import Trade, to_day

D0 = to_day(date(2026, 3, 2))  # a Monday
DAYS = np.array([D0, D0 + 1, D0 + 2, D0 + 5, D0 + 6])
EQUITY = np.array([100.0, 110.0, 99.0, 99.0, 120.0])
RF_DAYS, RF_PCT = np.array([D0 + 1, D0 + 5]), np.array([3.65, 7.3])


def curve(days=DAYS, equity=EQUITY) -> Curve:
    return Curve(np.asarray(days), np.asarray(equity, dtype=float), risk_free(np.asarray(days), RF_DAYS, RF_PCT))


def reference():
    """The quantities of §12, computed with the statistics module."""
    r = [b / a - 1 for a, b in zip(EQUITY, EQUITY[1:])]
    per_year = len(r) / (6 / 365.25)
    rf = [3.65, 3.65, 7.3, 7.3]
    e = [x - y / 100 / per_year for x, y in zip(r, rf)]
    return r, per_year, e


def test_the_risk_free_rate_is_forward_filled_and_zero_before_dtb3():
    assert risk_free(DAYS, RF_DAYS, RF_PCT).tolist() == [0.0, 3.65, 3.65, 7.3, 7.3]
    assert risk_free(DAYS, np.array([], dtype=np.int64), np.array([])).tolist() == [0.0] * 5


def test_periods_per_year_come_from_calendar_days():
    assert periods_per_year(curve()) == pytest.approx(4 / (6 / 365.25))


def test_curve_metrics():
    r, per_year, e = reference()
    found = curve_metrics(curve())
    cagr = 1.2 ** (365.25 / 6) - 1
    downside = math.sqrt(statistics.fmean([min(x, 0) ** 2 for x in e]))
    assert found == {
        "total_return": pytest.approx(0.2),
        "cagr": pytest.approx(cagr),
        "volatility": pytest.approx(statistics.stdev(r) * math.sqrt(per_year)),
        "sharpe": pytest.approx(statistics.fmean(e) / statistics.stdev(e) * math.sqrt(per_year)),
        "sortino": pytest.approx(statistics.fmean(e) / downside * math.sqrt(per_year)),
        "max_drawdown": pytest.approx(0.1),
        "max_drawdown_days": 5,
        "calmar": pytest.approx(cagr / 0.1),
    }


def test_an_unrecovered_drawdown_lasts_until_the_end():
    found = curve_metrics(curve([D0, D0 + 1, D0 + 3, D0 + 10], [100.0, 120.0, 90.0, 100.0]))
    assert found["max_drawdown"] == pytest.approx(0.25)
    assert found["max_drawdown_days"] == 9
    assert drawdown(np.array([100.0, 120.0, 90.0, 100.0])).tolist() == pytest.approx([0, 0, 0.25, 1 - 100 / 120])


def test_undefined_values_are_null():
    single = curve_metrics(curve([D0], [100.0]))
    assert single == {"total_return": 0.0, "cagr": None, "volatility": None, "sharpe": None, "sortino": None,
                      "max_drawdown": 0.0, "max_drawdown_days": 0, "calmar": None}
    flat = curve_metrics(Curve(DAYS, np.full(5, 100.0), np.zeros(5)))
    assert (flat["volatility"], flat["sharpe"], flat["sortino"], flat["calmar"]) == (0.0, None, None, None)
    assert trade_metrics([]) == {"trades": 0, "open_trades": 0, "win_rate": None, "payoff_ratio": None, "profit_factor": None,
                                 "expectancy": None, "expectancy_pct": None}


def trade(pnl: float, return_pct: float, reason: str = "SIGNAL") -> Trade:
    return Trade("A", "LONG", 10, D0, 10.0, D0 + 1, 11.0, pnl, return_pct, 1.0, reason)


def test_trade_metrics_count_closed_trades_only():
    trades = [trade(100, 10), trade(-50, -5), trade(30, 3), trade(-20, -2), trade(0, 0), trade(999, 99, "OPEN")]
    assert trade_metrics(trades) == {
        "trades": 5, "open_trades": 1, "win_rate": pytest.approx(2 / 5), "payoff_ratio": pytest.approx(65 / 35),
        "profit_factor": pytest.approx(130 / 70), "expectancy": pytest.approx(12.0), "expectancy_pct": pytest.approx(1.2),
    }
    only_wins = trade_metrics([trade(10, 1), trade(20, 2)])
    assert (only_wins["profit_factor"], only_wins["payoff_ratio"], only_wins["win_rate"]) == (None, None, 1.0)
    assert trade_metrics([trade(-10, -1)])["profit_factor"] == 0.0


def test_exposure_time_in_market_and_turnover():
    gross = np.array([0.0, 0.5, 1.0, 0.5, 0.0])
    positions = np.array([0, 1, 2, 1, 0])
    found = activity_metrics(curve(), gross, positions, traded_value=500.0)
    assert found == {
        "exposure": pytest.approx(0.4),
        "time_in_market": pytest.approx(0.6),
        "turnover": pytest.approx(500 / 2 / statistics.fmean(EQUITY) / (6 / 365.25)),
    }


def test_alpha_beta_and_correlation_against_the_benchmark():
    r, per_year, e = reference()
    benchmark_equity = [100.0, 104.0, 101.0, 103.0, 108.0]
    rb = [b / a - 1 for a, b in zip(benchmark_equity, benchmark_equity[1:])]
    rf = [x - y for x, y in zip(r, e)]
    eb = [x - y for x, y in zip(rb, rf)]
    beta = statistics.covariance(e, eb) / statistics.variance(eb)
    found = relative_metrics(curve(), curve(equity=benchmark_equity))
    assert found == {
        "alpha": pytest.approx((statistics.fmean(e) - beta * statistics.fmean(eb)) * per_year),
        "beta": pytest.approx(beta),
        "correlation": pytest.approx(statistics.correlation(r, rb)),
    }


def test_a_partial_benchmark_counts_only_common_points():
    late = curve(equity=[math.nan, math.nan, 100.0, 101.0, 103.0])
    found = relative_metrics(curve(), late)
    assert found["beta"] is not None and found["correlation"] is not None
    assert relative_metrics(curve(), curve(equity=[math.nan] * 4 + [100.0])) == {"alpha": None, "beta": None, "correlation": None}
    assert curve(equity=[math.nan, math.nan, 100.0, 101.0, 103.0]).since_first_value().days.tolist() == DAYS[2:].tolist()


def test_annual_and_monthly_returns_compound():
    days = [to_day(date(2025, 12, 30)), to_day(date(2025, 12, 31)), to_day(date(2026, 1, 2)), to_day(date(2026, 1, 30)),
            to_day(date(2026, 2, 2))]
    equity = [100.0, 102.0, 101.0, 106.05, 111.3525]
    tables = curve(days, equity)
    assert annual_returns(tables) == [{"year": 2025, "return": pytest.approx(0.02)},
                                      {"year": 2026, "return": pytest.approx(111.3525 / 102 - 1)}]
    assert monthly_returns(tables) == [
        {"year": 2025, "month": 12, "return": pytest.approx(0.02)},
        {"year": 2026, "month": 1, "return": pytest.approx(106.05 / 102 - 1)},
        {"year": 2026, "month": 2, "return": pytest.approx(111.3525 / 106.05 - 1)},
    ]
