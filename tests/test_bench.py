"""The bench builds its synthetic data and runs its cases; here only at a tiny size (SPEC.md §24.3, AC-17)."""

import numpy as np

from btlab import bench
from fakes import InlinePool


def test_the_bench_builds_daily_data_with_dividends_and_splits():
    config, data = bench.daily_setup(symbols=2, years=8)
    assert data.aligned.close.shape == (len(bench.weekdays(8)), 2) and config.symbols == ("S000", "S001")
    assert (data.aligned.cash > 0).any() and (data.aligned.ratio == 2.0).any()
    assert config.exits.ibguard_tp_pct == 15 and config.price_mode == "REALISTIC"


def test_the_bench_builds_minute_data_with_matching_daily_bars():
    config, data = bench.minute_setup(symbols=1, years=0.02)
    minutes = data.minutes[0]
    assert len(minutes.t) == len(data.aligned.days) * bench.SESSION_MINUTES
    assert np.allclose(data.aligned.close[:, 0], minutes.close.reshape(-1, bench.SESSION_MINUTES)[:, -1])
    assert config.timeframe == "1m" and config.intraday == {"close_out": "SESSION_END", "close_out_minutes": 15}


def test_the_bench_cases_run_at_a_tiny_size():
    assert bench.daily_case(symbols=2, years=1) > 0
    assert bench.grid_case(symbols=2, years=1, fast=range(5, 7), slow=range(20, 22), pool=InlinePool, workers=1) > 0
    assert bench.minute_case(symbols=1, years=0.02) > 0
