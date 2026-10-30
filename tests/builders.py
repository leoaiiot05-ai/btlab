"""Builders of hand-made engine inputs, shared by the engine, exits and later tests."""

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from btlab.costs import CostModel
from btlab.engine import Book, Settings, Targets, Timeline
from btlab.markets import PerShareCommission, RateCommission
from btlab.records import Action, DailyBars, RiskFreeRates, to_day
from btlab.store import Store
from btlab.taxes import load_taxes

TAXES = load_taxes(Path(__file__).resolve().parents[1] / "taxes.toml")
DAY0 = to_day(date(2020, 6, 1))  # a Monday
FREE_US = PerShareCommission(per_share=0.0, min=0.0, max_pct=1.0)
FREE_TW = RateCommission(rate_pct=0.0, min=0.0)
NOW = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)
FIRST, LAST = to_day(date(2019, 1, 1)), to_day(date(2020, 12, 31))
WEEKDAYS = np.array([day for day in range(FIRST, LAST + 1) if (day + 3) % 7 < 5], dtype=np.int64)


def bars(closes, opens=None, highs=None, lows=None, *, days=None, sessions=None, tradable=None, lock=None, cash=None,
         ratio=None, adjust=None, start=0, symbols=None) -> Timeline:
    """A timeline from bars × symbols rows; each bar is its own session on consecutive days unless told otherwise."""
    close = np.array(closes, dtype=np.float64)
    count, width = close.shape
    open_ = close.copy() if opens is None else np.array(opens, dtype=np.float64)
    high = np.fmax(open_, close) if highs is None else np.array(highs, dtype=np.float64)
    low = np.fmin(open_, close) if lows is None else np.array(lows, dtype=np.float64)
    adjust = np.ones_like(close) if adjust is None else np.array(adjust, dtype=np.float64)
    days = np.arange(DAY0, DAY0 + count) if days is None else np.array(days)
    session = np.arange(count) if sessions is None else np.array(sessions)
    return Timeline(
        symbols=symbols or tuple("ABCD"[:width]), time=days.copy(), day=days, session=session,
        open=open_, high=high, low=low, close=close,
        adj_open=open_ * adjust, adj_high=high * adjust, adj_low=low * adjust, adj_close=close * adjust,
        tradable=np.isfinite(close) if tradable is None else np.array(tradable, dtype=bool),
        lock=np.zeros(close.shape, dtype=np.int8) if lock is None else np.array(lock, dtype=np.int8),
        cash=np.zeros_like(close) if cash is None else np.array(cash, dtype=np.float64),
        ratio=np.ones_like(close) if ratio is None else np.array(ratio, dtype=np.float64), start=start,
    )


def us_costs(width=1, commission=FREE_US, slippage=0.0, withholding=0.0, borrow=0.5) -> CostModel:
    return CostModel("US", commission, slippage, withholding, ("STOCK",) * width, borrow_fee_pct=(borrow,) * width)


def tw_costs(categories=("STOCK",), commission=FREE_TW, short_fee=0.08, taxes=TAXES) -> CostModel:
    return CostModel("TW", commission, 0.0, 0.0, tuple(categories), taxes, short_fee_pct=short_fee)


def settings(costs=None, market="US", capital=10_000.0, **options) -> Settings:
    return Settings(market, capital, costs or us_costs(), **options)


def direction(*rows) -> Targets:
    return Targets("DIRECTION", np.array(rows, dtype=np.float64))


def weights(*rows, rebalance=None) -> Targets:
    return Targets("WEIGHTS", np.array(rows, dtype=np.float64), None if rebalance is None else np.array(rebalance, dtype=bool))


class Script:
    """An ORDERS strategy: at bar t it calls the Book methods listed for t."""

    def __init__(self, plan: dict[int, list[tuple[str, int, float]]]) -> None:
        self.plan = plan

    def on_bar(self, book: Book) -> None:
        for method, j, value in self.plan.get(book.t, []):
            getattr(book, method)(j, value)


def walk(seed: int, count: int) -> np.ndarray:
    """A random walk of closes from 100, with a fixed seed."""
    return 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0.0003, 0.01, count)))


def put_bars(store: Store, market: str, symbol: str, days: np.ndarray, close: np.ndarray) -> None:
    category = "ETF" if symbol in ("SPY", "0050") else "STOCK"
    store.add_symbol(market, symbol, category, NOW)
    store.upsert_daily_bars(market, symbol, DailyBars(days, close * 0.999, close * 1.01, close * 0.99, close,
                                                      np.full(len(days), 1e6), np.zeros(len(days), dtype=bool)))


def synthetic_store(folder: Path) -> Store:
    """Two years of weekdays: SPY, AAA (with a dividend and a split) and BBB in US; 0050 and 2330 in TW; DTB3 at 2%."""
    store = Store(folder)
    for seed, symbol in enumerate(("SPY", "AAA", "BBB")):
        put_bars(store, "US", symbol, WEEKDAYS, walk(seed, len(WEEKDAYS)))
    store.replace_actions("US", "AAA", FIRST, LAST, [Action(int(WEEKDAYS[300]), "CASH_DIV", "DIV_RESULT", cash=0.5),
                                                     Action(int(WEEKDAYS[400]), "SHARE_RATIO", "SPLIT", ratio=2.0)])
    store.upsert_rf_rates(RiskFreeRates(WEEKDAYS[::5], np.full(len(WEEKDAYS[::5]), 2.0)))
    store.replace_trading_days("TW", WEEKDAYS)
    for seed, symbol in enumerate(("0050", "2330"), 10):
        put_bars(store, "TW", symbol, WEEKDAYS, walk(seed, len(WEEKDAYS)))
    return store
