"""Timing on production-size synthetic data (SPEC.md §24.3): `python -m btlab.bench` prints the time of each case.

The data are made in memory, without a store: random walks with quarterly dividends and a split every seven years, and
1-minute bars for every session. The test suite only imports this module and runs it at a tiny size.
"""

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date

import numpy as np

from btlab.backtest import WARMUP_BARS, RunConfig, RunData, run_backtest, strategy_factory, validate_config
from btlab.calendar import session
from btlab.intraday import WARMUP_SESSIONS
from btlab.optimize import PoolFactory, default_workers, optimize, parse_request, process_pool
from btlab.prices import Aligned, align
from btlab.records import Action, DailyBars, MinuteBars, to_date, to_day

LAST_DAY = to_day(date(2026, 9, 30))
SEED = 2026
DIVIDEND_EVERY = 63  # bars: quarterly
SPLIT_EVERY = 1_764  # bars: about seven years
SESSION_MINUTES = 390
MINUTE_RULE = {
    "version": 1, "timeframe": "1m", "side": "long",
    "entry": {"all": [
        {"left": {"ind": "PRICE"}, "op": "cross_above", "right": {"ind": "SMA", "args": {"n": 5}, "tf": "1d"}},
        {"left": {"ind": "RSI", "args": {"n": 14}}, "op": "lt", "right": {"value": 70}},
    ]},
    "exit": {"left": {"ind": "PRICE"}, "op": "lt", "right": {"ind": "PRICE", "src": "low", "tf": "1d"}},
}


def weekdays(years: float) -> np.ndarray:
    """The weekdays of the years up to LAST_DAY; holidays play no part in timing."""
    days = np.arange(LAST_DAY - round(365.25 * years), LAST_DAY + 1)
    return days[(days + 3) % 7 < 5]


def daily_setup(symbols: int = 50, years: float = 30) -> tuple[RunConfig, RunData]:
    """Case 1: MA_CROSS with the ibguard exit, REALISTIC, on walks with dividends and splits."""
    days = weekdays(years)
    rng = np.random.default_rng(SEED)
    names = [f"S{j:03d}" for j in range(symbols)]
    aligned, _ = align("US", days, {name: _history(days, rng) for name in names})
    benchmark, _ = align("US", days, {"SPY": _history(days, rng)})
    start = min(WARMUP_BARS, len(days) // 2)
    config = validate_config({
        "name": "bench", "market": "US", "symbols": names, "start": _iso(days[start]),
        "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 20, "slow": 50}},
        "exits": {"ibguard": {"tp_pct": 15, "sl_pct": 5}}, "price_mode": "REALISTIC",
    })
    return config, _run_data(aligned, benchmark, start)


def minute_setup(symbols: int = 10, years: float = 3) -> tuple[RunConfig, RunData]:
    """Case 3: a 1-minute rule against the previous session's daily values, closed out 15 minutes before the end."""
    days = weekdays(years)
    rng = np.random.default_rng(SEED)
    opens = np.array([session("US", int(day))[0] for day in days], dtype=np.int64)
    times = (opens[:, None] + 60 * np.arange(SESSION_MINUTES)).ravel()
    names = [f"M{j:03d}" for j in range(symbols)]
    minutes = tuple(_minute_walk(times, rng) for _ in names)
    aligned, _ = align("US", days, {name: (_daily_of(days, bars), []) for name, bars in zip(names, minutes)})
    benchmark, _ = align("US", days, {"SPY": _history(days, rng)})
    start = min(WARMUP_SESSIONS, len(days) - 1)
    config = validate_config({
        "name": "bench", "market": "US", "symbols": names, "start": _iso(days[start]), "timeframe": "1m",
        "strategy": {"source": "rule", "rule": MINUTE_RULE},
        "intraday": {"close_out": "SESSION_END", "close_out_minutes": 15},
    })
    data = replace(_run_data(aligned, benchmark, start), minutes=minutes, covered=tuple(days for _ in names))
    return config, data


def daily_case(symbols: int = 50, years: float = 30) -> float:
    config, data = daily_setup(symbols, years)
    return _seconds(lambda: run_backtest(config, data, None, strategy_factory(config)))


def grid_case(symbols: int = 50, years: float = 30, fast: range = range(5, 55), slow: range = range(60, 100),
              pool: PoolFactory | None = None, workers: int | None = None) -> float:
    """Case 2: case 1 over fast 5 to 54 and slow 60 to 99, 2,000 combinations, in the real pool."""
    config, data = daily_setup(symbols, years)
    workers = workers or default_workers()
    request = parse_request({"config": config.document, "confirm_long": True, "ranges": {
        "strategy.params.fast": {"values": list(fast)}, "strategy.params.slow": {"values": list(slow)},
    }})
    return _seconds(lambda: optimize(request, data, None, pool or process_pool(workers), workers))


def minute_case(symbols: int = 10, years: float = 3) -> float:
    config, data = minute_setup(symbols, years)
    return _seconds(lambda: run_backtest(config, data, None, strategy_factory(config)))


CASES = (
    ("1. one daily run, 50 symbols × 30 years", daily_case, 5.0),
    ("2. a grid of 2,000 combinations of case 1", grid_case, 600.0),
    ("3. one 1-minute run, 10 symbols × 3 years", minute_case, 60.0),
)


def main() -> None:
    for name, case, target in CASES:
        print(f"{name}: {case():.1f} s (target {target:g} s)", flush=True)


def _history(days: np.ndarray, rng: np.random.Generator) -> tuple[DailyBars, list[Action]]:
    """A walk in adjusted terms: raw prices are its multiples by the later splits, and dividends pay 0.5% a quarter."""
    count = len(days)
    walk = 50 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, count)))
    splits = np.arange(SPLIT_EVERY, count, SPLIT_EVERY)
    raw = walk * 2.0 ** (len(splits) - np.searchsorted(splits, np.arange(count), side="right"))
    actions = [Action(int(days[s]), "SHARE_RATIO", "SPLIT", ratio=2.0) for s in splits]
    actions += [Action(int(days[d]), "CASH_DIV", "DIV_RESULT", cash=round(0.005 * raw[d - 1], 4))
                for d in range(DIVIDEND_EVERY, count, DIVIDEND_EVERY)]
    bars = DailyBars(days, raw * 0.999, raw * 1.01, raw * 0.99, raw, rng.uniform(1e5, 1e6, count), np.zeros(count, dtype=bool))
    return bars, sorted(actions, key=lambda action: action.day)


def _minute_walk(times: np.ndarray, rng: np.random.Generator) -> MinuteBars:
    close = 50 * np.exp(np.cumsum(rng.normal(0.0, 0.0008, len(times))))
    return MinuteBars(times, close * 0.9995, close * 1.001, close * 0.999, close, rng.uniform(100.0, 1_000.0, len(times)))


def _daily_of(days: np.ndarray, bars: MinuteBars) -> DailyBars:
    """Each session's daily bar from its 1-minute bars."""
    shape = (len(days), SESSION_MINUTES)
    high, low = bars.high.reshape(shape).max(axis=1), bars.low.reshape(shape).min(axis=1)
    return DailyBars(days, bars.open.reshape(shape)[:, 0], high, low, bars.close.reshape(shape)[:, -1],
                     bars.volume.reshape(shape).sum(axis=1), np.zeros(len(days), dtype=bool))


def _run_data(aligned: Aligned, benchmark: Aligned, start: int) -> RunData:
    count = len(aligned.symbols)
    return RunData(aligned, start, benchmark, "ETF", np.full(len(aligned.days), 2.0), ("STOCK",) * count,
                   (None,) * count, ((),) * count, {})


def _seconds(work: Callable[[], object]) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def _iso(day: int) -> str:
    return to_date(int(day)).isoformat()


if __name__ == "__main__":
    main()
