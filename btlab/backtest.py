"""One backtest: configuration, data, the engine, the benchmark, metrics and the result (SPEC.md §10.1, §10.9, §10.11,
§12, §17.5).

load_run reads the stores; run_backtest is pure given the loaded data, so a pool worker can run it.
"""

import csv
import io
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from btlab.calendar import session
from btlab.costs import CostModel
from btlab.engine import EngineResult, OrderStrategy, Settings, Targets, daily_timeline, simulate
from btlab.exits import DailyMinutePaths, ExitRules, Overlays
from btlab.features import session_bars
from btlab.markets import MARKETS, PerShareCommission, RateCommission, category_by_rule, normalize_symbol
from btlab.metrics import (
    Curve,
    activity_metrics,
    annual_returns,
    curve_metrics,
    drawdown,
    monthly_returns,
    relative_metrics,
    risk_free,
    trade_metrics,
)
from btlab.prices import Aligned, align
from btlab.records import MinuteBars, ShortSuspension, Trade, to_date, to_day
from btlab.sizing import SIZING_MODES, Sizing
from btlab.store import MinuteStore, Store
from btlab.taxes import TaxTable

# Daily data load from this many bars before the start, for the indicators' warm-up (§10.1).
WARMUP_BARS = 300
TIMEFRAMES = ("1d",)
STRATEGY_SOURCES = ("builtin", "plugin", "rule")
PRICE_MODES = ("REALISTIC", "ADJUSTED")
FILL_TIMINGS = ("NEXT_OPEN", "SAME_CLOSE")
INTRABAR_PATHS = ("DAILY", "MINUTE")
TRADES_COLUMNS = ("symbol", "side", "quantity", "entry time", "entry price", "exit time", "exit price", "P&L", "return %",
                  "costs", "exit reason")
EQUITY_COLUMNS = ("time", "equity", "benchmark", "drawdown", "gross exposure")


class RunConfigError(Exception):
    """A run configuration has invalid fields; each error names its JSON path (INVALID_FIELD, §10.1)."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(f"{path}: {message}" for path, message in errors))


class RunError(Exception):
    """A run that cannot proceed, with its code (NO_DATA)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RunConfig:
    """A validated run configuration (§10.1). document is the complete JSON, defaults filled in, as stored."""

    document: dict
    name: str
    market: str
    symbols: tuple[str, ...]
    start: int
    end: int | None
    timeframe: str
    strategy: dict
    sizing: Sizing
    exits: ExitRules
    price_mode: str
    fill_timing: str
    intrabar_path: str
    allow_short: bool
    board_lots_only: bool
    initial_capital: float
    max_position_pct: float
    commission: PerShareCommission | RateCommission
    slippage_bps: float
    borrow_fee_pct: float
    dividend_withholding_pct: float
    short_fee_pct: float
    benchmark: str


@dataclass(frozen=True, eq=False)
class RunData:
    """What a run reads, loaded from the stores. aligned holds the run's symbols from the warm-up on, start is the first
    bar of trading, rf the DTB3 rate in force on each day (§12), categories and borrow_fee_pct each symbol's settings
    (§7, None for the run's default). minutes and covered serve the MINUTE intrabar path."""

    aligned: Aligned
    start: int
    benchmark: Aligned
    benchmark_category: str
    rf: np.ndarray
    categories: tuple[str, ...]
    borrow_fee_pct: tuple[float | None, ...]
    suspensions: tuple[tuple[ShortSuspension, ...], ...]
    data_stamp: dict
    minutes: tuple[MinuteBars, ...] | None = None
    covered: tuple[np.ndarray, ...] | None = None


@dataclass(frozen=True, eq=False)
class BacktestResult:
    """A run's result (§10.11): the series of §18, the trades, metrics and tables of the strategy and its benchmark,
    the flags with their details, and the data stamp."""

    config: dict
    time_unit: str
    series: dict[str, np.ndarray]
    trades: tuple[Trade, ...]
    metrics: dict
    tables: dict
    flags: dict
    data_stamp: dict


StrategyFactory = Callable[[RunData], "Targets | OrderStrategy"]


# ----- the configuration (§10.1) -----


def validate_config(raw: Any) -> RunConfig:
    """Checks every field and fills in the market's defaults; raises RunConfigError listing every invalid field."""
    errors: list[tuple[str, str]] = []
    if not isinstance(raw, dict):
        raise RunConfigError([("", "the configuration must be an object")])
    top = _Object(raw, "", errors, _TOP_KEYS)
    market = top.get("market", _choice(*MARKETS))
    spec = MARKETS.get(market)
    if spec is None:
        raise RunConfigError(errors)
    name = top.get("name", _text)
    symbols = _symbols(market, raw.get("symbols"), errors)
    list_id = top.get("list_id", _optional(_integer(1, None)), None)
    start = top.get("start", _day)
    end = top.get("end", _optional(_day), None)
    if start is not None and end is not None and end < start:
        errors.append(("end", "must not be before start"))
    timeframe = top.get("timeframe", _choice(*TIMEFRAMES), "1d")
    top.get("intraday", _null, None)
    strategy = _strategy(raw.get("strategy"), errors)
    sizing = _sizing(raw.get("sizing", {"mode": "FIXED_FRACTION"}), errors)
    exits = _exits(raw.get("exits", {}), errors)
    costs = _costs(market, raw.get("costs", {}), errors)
    values = {
        "price_mode": top.get("price_mode", _choice(*PRICE_MODES), "REALISTIC"),
        "fill_timing": top.get("fill_timing", _choice(*FILL_TIMINGS), "NEXT_OPEN"),
        "intrabar_path": top.get("intrabar_path", _choice(*INTRABAR_PATHS), "DAILY"),
        "allow_short": top.get("allow_short", _flag, False),
        "board_lots_only": top.get("board_lots_only", _flag, False),
        "initial_capital": top.get("initial_capital", _number(0, None, low_open=True), spec.initial_capital),
        "max_position_pct": top.get("max_position_pct", _number(0, 100, low_open=True), 100.0),
        "benchmark": top.get("benchmark", _symbol(market), spec.benchmark),
    }
    if errors:
        raise RunConfigError(errors)
    document = {
        "name": name, "market": market, "symbols": list(symbols), "list_id": list_id, "start": _iso(start),
        "end": _iso(end) if end is not None else None, "timeframe": timeframe, "strategy": strategy,
        "sizing": sizing[1], "exits": exits[1], **values, "costs": costs[1], "intraday": None,
    }
    commission, slippage, borrow, withholding, short_fee = costs[0]
    return RunConfig(
        document=document, name=name, market=market, symbols=symbols, start=start, end=end, timeframe=timeframe,
        strategy=strategy, sizing=sizing[0], exits=exits[0], commission=commission, slippage_bps=slippage,
        borrow_fee_pct=borrow, dividend_withholding_pct=withholding, short_fee_pct=short_fee, **values,
    )


_TOP_KEYS = {
    "name", "market", "symbols", "list_id", "start", "end", "timeframe", "strategy", "sizing", "exits", "price_mode",
    "fill_timing", "intrabar_path", "allow_short", "board_lots_only", "initial_capital", "max_position_pct", "costs",
    "benchmark", "intraday",
}
_REQUIRED = object()


class _Invalid(ValueError):
    """A value breaks a rule; the message says which."""


class _Object:
    """One object of the configuration: each read records an error with the field's JSON path."""

    def __init__(self, document: Any, path: str, errors: list[tuple[str, str]], keys: set[str]) -> None:
        self.path, self.errors = path, errors
        if not isinstance(document, dict):
            if document is not None:
                errors.append((path, "must be an object"))
            document = {}
        self.document = document
        errors.extend((self.at(key), "is not a known field") for key in document if key not in keys)

    def at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def get(self, key: str, check: Callable[[Any], Any], default: Any = _REQUIRED) -> Any:
        if key not in self.document:
            if default is _REQUIRED:
                self.errors.append((self.at(key), "is required"))
            return None if default is _REQUIRED else default
        try:
            return check(self.document[key])
        except _Invalid as invalid:
            self.errors.append((self.at(key), str(invalid)))
            return None


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("must be a non-empty string")
    return value.strip()


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _Invalid("must be true or false")
    return value


def _null(value: Any) -> None:
    if value is not None:
        raise _Invalid("must be null on the daily timeline")


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in options:
            raise _Invalid("must be one of " + ", ".join(options))
        return value

    return check


def _number(low: float | None, high: float | None, low_open: bool = False, high_open: bool = False) -> Callable[[Any], float]:
    """A finite number, not a boolean, within [low, high]; an open bound excludes its end."""

    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _Invalid("must be a finite number")
        if low is not None and (value < low or (low_open and value == low)):
            raise _Invalid(f"must be {'above' if low_open else 'at least'} {low:g}")
        if high is not None and (value > high or (high_open and value == high)):
            raise _Invalid(f"must be {'below' if high_open else 'at most'} {high:g}")
        return float(value)

    return check


def _integer(low: int, high: int | None) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _Invalid("must be a whole number")
        _number(low, high)(value)
        return value

    return check


def _optional(check: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda value: None if value is None else check(value)


def _day(value: Any) -> int:
    try:
        return to_day(date.fromisoformat(value))
    except (TypeError, ValueError):
        raise _Invalid('must be a date "YYYY-MM-DD"') from None


def _iso(day: int) -> str:
    return to_date(day).isoformat()


def _symbol(market: str) -> Callable[[Any], str]:
    """A symbol by §7's rules, normalized."""

    def check(value: Any) -> str:
        try:
            return normalize_symbol(market, value if isinstance(value, str) else "")
        except ValueError:
            raise _Invalid(f"is not a {market} symbol") from None

    return check


def _symbols(market: str, value: Any, errors: list[tuple[str, str]]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        errors.append(("symbols", "must be a non-empty list of symbols"))
        return ()
    found: list[str] = []
    for index, text in enumerate(value):
        try:
            symbol = _symbol(market)(text)
        except _Invalid as invalid:
            errors.append((f"symbols[{index}]", str(invalid)))
            continue
        if symbol in found:
            errors.append((f"symbols[{index}]", "appears twice"))
        else:
            found.append(symbol)
    return tuple(found)


def _strategy(value: Any, errors: list[tuple[str, str]]) -> dict:
    """The source, its id or rule, and an object of parameters; each source checks its parameters (prompts 9, 10, 12)."""
    if value is None:
        errors.append(("strategy", "is required"))
        return {}
    fields = _Object(value, "strategy", errors, {"source", "id", "params", "rule"})
    source = fields.get("source", _choice(*STRATEGY_SOURCES))
    document = {"source": source, "params": fields.get("params", _object, {})}
    if source == "rule":
        document["rule"] = fields.get("rule", _object)
    elif source is not None:
        document["id"] = fields.get("id", _text)
    return document


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise _Invalid("must be an object")
    return value


_SIZING_FIELDS = {
    "FIXED_FRACTION": {"fraction": (_optional(_number(0, 1, low_open=True)), None)},
    "FIXED_AMOUNT": {"amount": (_number(0, None, low_open=True), _REQUIRED)},
    "FIXED_SHARES": {"shares": (_number(0, None, low_open=True), _REQUIRED)},
    "VOL_RISK": {
        "risk_pct": (_number(0, 100, low_open=True), 1.0),
        "atr_n": (_integer(1, 1000), 20),
        "atr_mult": (_number(0, None, low_open=True), 2.0),
    },
}


def _sizing(value: Any, errors: list[tuple[str, str]]) -> tuple[Sizing, dict]:
    mode = value.get("mode") if isinstance(value, dict) else None
    fields = _Object(value, "sizing", errors, {"mode", *_SIZING_FIELDS.get(mode, {})})
    mode = fields.get("mode", _choice(*SIZING_MODES), "FIXED_FRACTION")
    document = {"mode": mode or "FIXED_FRACTION"}
    for key, (check, default) in _SIZING_FIELDS.get(mode, {}).items():
        document[key] = fields.get(key, check, default)
    return Sizing(**document), document


def _exits(value: Any, errors: list[tuple[str, str]]) -> tuple[ExitRules, dict]:
    keys = {"stop_loss_pct", "take_profit_pct", "trailing_pct", "atr_stop", "max_hold_bars", "ibguard"}
    fields = _Object(value, "exits", errors, keys)
    below_100 = _optional(_number(0, 100, low_open=True, high_open=True))
    document = {
        "stop_loss_pct": fields.get("stop_loss_pct", below_100, None),
        "take_profit_pct": fields.get("take_profit_pct", _optional(_number(0, None, low_open=True)), None),
        "trailing_pct": fields.get("trailing_pct", below_100, None),
        "atr_stop": None,
        "max_hold_bars": fields.get("max_hold_bars", _optional(_integer(1, None)), None),
        "ibguard": None,
    }
    atr = fields.document.get("atr_stop")
    if atr is not None:
        atr_fields = _Object(atr, "exits.atr_stop", errors, {"n", "mult"})
        document["atr_stop"] = {"n": atr_fields.get("n", _integer(1, 1000)), "mult": atr_fields.get("mult", _number(0, None, low_open=True))}
    guard = fields.document.get("ibguard")
    if guard is not None:
        # ibguard's own ranges (§16.3).
        guard_fields = _Object(guard, "exits.ibguard", errors, {"tp_pct", "sl_pct"})
        document["ibguard"] = {
            "tp_pct": guard_fields.get("tp_pct", _number(0, 1000, low_open=True)),
            "sl_pct": guard_fields.get("sl_pct", below_100),
        }
    atr_stop, guard = document["atr_stop"] or {}, document["ibguard"] or {}
    rules = ExitRules(
        stop_loss_pct=document["stop_loss_pct"], take_profit_pct=document["take_profit_pct"],
        trailing_pct=document["trailing_pct"], atr_n=atr_stop.get("n"), atr_mult=atr_stop.get("mult"),
        max_hold_bars=document["max_hold_bars"], ibguard_tp_pct=guard.get("tp_pct"), ibguard_sl_pct=guard.get("sl_pct"),
    )
    return rules, document


def _costs(market: str, value: Any, errors: list[tuple[str, str]]) -> tuple[tuple, dict]:
    """The market's cost fields, each defaulting to §4.1's value (§10.1, §10.6)."""
    spec = MARKETS[market]
    us = market == "US"
    keys = {"commission", "slippage_bps", "dividend_withholding_pct", "borrow_fee_pct" if us else "short_fee_pct"}
    fields = _Object(value, "costs", errors, keys)
    commission = _commission(spec.commission, fields.document.get("commission"), errors)
    at_least_0 = _number(0, None)
    document = {
        "commission": {k: getattr(commission, k) for k in commission.__dataclass_fields__} if commission else None,
        "slippage_bps": fields.get("slippage_bps", at_least_0, spec.slippage_bps),
        "dividend_withholding_pct": fields.get("dividend_withholding_pct", _number(0, 100), spec.dividend_withholding_pct),
    }
    if us:
        document["borrow_fee_pct"] = fields.get("borrow_fee_pct", at_least_0, spec.borrow_fee_pct)
    else:
        document["short_fee_pct"] = fields.get("short_fee_pct", at_least_0, spec.short_fee_pct)
    values = (commission, document["slippage_bps"], document.get("borrow_fee_pct", 0.0),
              document["dividend_withholding_pct"], document.get("short_fee_pct", 0.0))
    return values, document


def _commission(default: PerShareCommission | RateCommission, value: Any, errors: list[tuple[str, str]]):
    if value is None:
        return default
    names = tuple(default.__dataclass_fields__)
    fields = _Object(value, "costs.commission", errors, set(names))
    found = {name: fields.get(name, _number(0, 100) if name.endswith("pct") else _number(0, None), getattr(default, name))
             for name in names}
    return None if any(v is None for v in found.values()) else type(default)(**found)


# ----- loading (I/O) -----


def load_run(store: Store, minute_store: MinuteStore | None, config: RunConfig) -> RunData:
    """The run's data from the stores: every bar and action from the warm-up through the latest stored day, because
    adjusted prices are relative to it (§6.3). Raises RunError NO_DATA when the period holds no bar."""
    market = config.market
    calendar = store.daily_bars("US", "SPY").day if market == "US" else store.trading_days("TW")
    ranges = {symbol: store.daily_bar_range(market, symbol) for symbol in config.symbols}
    latest = max((found[1] for found in ranges.values() if found), default=None)
    end = config.end if config.end is not None else latest
    calendar = calendar[calendar <= end] if end is not None else calendar[:0]
    first = int(np.searchsorted(calendar, config.start, side="left"))
    if latest is None or first >= len(calendar) or latest < config.start:
        raise RunError("NO_DATA", "no stored bar of the run's symbols falls in its period")
    days = calendar[max(0, first - WARMUP_BARS):]

    def history(symbol: str):
        return store.daily_bars(market, symbol, first_day=int(days[0])), store.actions(market, symbol)

    histories = {symbol: history(symbol) for symbol in config.symbols}
    aligned, _ = align(market, days, histories)
    benchmark, _ = align(market, days, {config.benchmark: history(config.benchmark)})
    infos = {info.symbol: info for info in store.symbols(market)}
    rates = store.rf_rates()
    data = {
        "aligned": aligned, "start": first - max(0, first - WARMUP_BARS), "benchmark": benchmark,
        "benchmark_category": _category(infos, market, config.benchmark),
        "rf": risk_free(days, rates.day, rates.rate_pct),
        "categories": tuple(_category(infos, market, symbol) for symbol in config.symbols),
        "borrow_fee_pct": tuple(infos[s].borrow_fee_pct if s in infos else None for s in config.symbols),
        "suspensions": tuple(tuple(store.short_suspensions(s)) if market == "TW" else () for s in config.symbols),
        "data_stamp": {
            symbol: {
                "bar": _iso(ranges[symbol][1]) if ranges[symbol] else None,
                "action": _iso(histories[symbol][1][-1].day) if histories[symbol][1] else None,
            }
            for symbol in config.symbols
        },
    }
    if config.intrabar_path == "MINUTE" and minute_store is not None:
        first_t, last_t = session(market, int(days[0]))[0], session(market, int(days[-1]))[1]
        data["minutes"] = tuple(minute_store.minute_bars(market, s, first_t, last_t) for s in config.symbols)
        data["covered"] = tuple(
            np.array([c.day for c in minute_store.minute_coverage(market, s, int(days[0]), int(days[-1])) if c.status == "OK"],
                     dtype=np.int64)
            for s in config.symbols
        )
    return RunData(**data)


def _category(infos: dict, market: str, symbol: str) -> str:
    return infos[symbol].category if symbol in infos else category_by_rule(market, symbol)


# ----- running (pure) -----


def run_backtest(config: RunConfig, data: RunData, taxes: TaxTable | None, strategy: StrategyFactory) -> BacktestResult:
    """The engine on the run's data, then the benchmark as BUY_HOLD of its symbol at 100% through the same engine
    (§10.9), the metrics of §12, the flags and the result."""
    timeline = daily_timeline(data.aligned, data.start)
    costs = _cost_model(config, data.categories, data.borrow_fee_pct, taxes)
    settings = _settings(config, costs, data.suspensions)
    result = simulate(timeline, strategy(data), settings, Overlays(config.exits, _minute_paths(config, data)))
    benchmark, partial = _benchmark(config, data, taxes)
    days = timeline.day[data.start:]
    rf = data.rf[data.start:]
    curve = Curve(days, result.equity, rf)
    benchmark_curve = Curve(days, benchmark.equity_or_nan, rf)
    flags = {"SURVIVORSHIP": None, **result.flags, **({"BENCHMARK_PARTIAL": None} if partial else {})}
    return BacktestResult(
        config=config.document,
        time_unit="day",
        series={
            "TIME": days.astype(np.float64), "EQUITY": result.equity, "BENCHMARK": benchmark.equity_or_nan,
            "CASH": result.cash, "GROSS": result.gross, "NET": result.net, "POSITIONS": result.positions.astype(np.float64),
        },
        trades=result.trades,
        metrics={
            "strategy": _metrics(curve, result) | relative_metrics(curve, benchmark_curve),
            "benchmark": _metrics(benchmark_curve.since_first_value(), benchmark.result, benchmark.first),
        },
        tables={"strategy": _tables(curve), "benchmark": _tables(benchmark_curve.since_first_value())},
        flags=flags,
        data_stamp=data.data_stamp,
    )


def _cost_model(config: RunConfig, categories: tuple[str, ...], borrow: tuple[float | None, ...], taxes: TaxTable | None) -> CostModel:
    us = config.market == "US"
    return CostModel(
        market=config.market, commission_rule=config.commission, slippage_bps=config.slippage_bps,
        dividend_withholding_pct=config.dividend_withholding_pct, categories=categories,
        taxes=None if us else taxes,
        borrow_fee_pct=tuple(config.borrow_fee_pct if fee is None else fee for fee in borrow) if us else (),
        short_fee_pct=0.0 if us else config.short_fee_pct,
    )


def _settings(config: RunConfig, costs: CostModel, suspensions: tuple) -> Settings:
    return Settings(
        market=config.market, initial_capital=config.initial_capital, costs=costs, sizing=config.sizing,
        price_mode=config.price_mode, fill_timing=config.fill_timing, allow_short=config.allow_short,
        board_lots_only=config.board_lots_only, max_position_pct=config.max_position_pct, suspensions=suspensions,
    )


def _minute_paths(config: RunConfig, data: RunData) -> DailyMinutePaths | None:
    """Minute paths for intrabar_path MINUTE: raw in REALISTIC mode, scaled to the adjusted series in ADJUSTED mode."""
    if config.intrabar_path != "MINUTE":
        return None
    aligned = data.aligned
    count = len(aligned.symbols)
    # Without a minute store nothing is covered, and every position-day falls back to the daily path.
    nothing = MinuteBars(np.zeros(0, dtype=np.int64), *(np.zeros(0) for _ in range(5)))
    minutes = data.minutes or (nothing,) * count
    covered = data.covered or (np.zeros(0, dtype=np.int64),) * count
    realistic = config.price_mode == "REALISTIC"
    return DailyMinutePaths([
        session_bars(config.market, aligned.days, bars, days, np.ones(len(aligned.days)) if realistic else aligned.multiplier[:, j])
        for j, (bars, days) in enumerate(zip(minutes, covered))
    ])


@dataclass(frozen=True, eq=False)
class _Benchmark:
    result: EngineResult
    equity_or_nan: np.ndarray
    first: int


def _benchmark(config: RunConfig, data: RunData, taxes: TaxTable | None) -> tuple[_Benchmark, bool]:
    """BUY_HOLD of the benchmark at 100% from its first bar in the period; partial when it lacks any part of it."""
    timeline = daily_timeline(data.benchmark, data.start)
    known = np.flatnonzero(np.isfinite(data.benchmark.close[data.start:, 0]))
    first = int(known[0]) if len(known) else len(timeline.day) - data.start
    weights = np.full(data.benchmark.close.shape, np.nan)
    if len(known):
        weights[data.start + first, 0] = 1.0
    costs = _cost_model(config, (data.benchmark_category,), (None,), taxes)
    settings = _settings(config, costs, ((),))
    result = simulate(timeline, Targets("WEIGHTS", weights), settings)
    equity = result.equity.copy()
    equity[:first] = np.nan
    partial = not len(known) or first > 0 or known[-1] < len(timeline.day) - data.start - 1
    return _Benchmark(result, equity, first), partial


def _metrics(curve: Curve, result: EngineResult, first: int = 0) -> dict:
    traded = sum(fill.quantity * fill.price for fill in result.fills)
    return curve_metrics(curve) | trade_metrics(result.trades) | activity_metrics(
        curve, result.gross[first:], result.positions[first:], traded
    )


def _tables(curve: Curve) -> dict:
    return {"annual": annual_returns(curve), "monthly": monthly_returns(curve)}


def save_result(store: Store, result: BacktestResult, created_at: datetime) -> int:
    """Saves a result in one transaction (§17.5); returns its id."""
    config = result.config
    return store.save_backtest(
        created_at, config["name"], config["market"], config["timeframe"], config, result.data_stamp, result.metrics,
        result.tables, result.flags, result.series, result.trades,
    )


# ----- CSV exports (§12) -----


def trades_csv(trades: tuple[Trade, ...], market: str, time_unit: str) -> bytes:
    """trades.csv: UTF-8 with a BOM, so Excel shows Chinese names; times in the market's time zone."""
    rows = [
        (t.symbol, t.side, _cell(t.quantity), _time(t.entry_time, market, time_unit), _cell(t.entry_price),
         _time(t.exit_time, market, time_unit), _cell(t.exit_price), _cell(t.pnl), _cell(t.return_pct), _cell(t.costs),
         t.exit_reason)
        for t in trades
    ]
    return _csv(TRADES_COLUMNS, rows)


def equity_csv(series: dict[str, np.ndarray], market: str, time_unit: str) -> bytes:
    equity = series["EQUITY"]
    rows = zip(
        (_time(int(t), market, time_unit) for t in series["TIME"]), map(_cell, equity), map(_cell, series["BENCHMARK"]),
        map(_cell, drawdown(equity)), map(_cell, series["GROSS"]),
    )
    return _csv(EQUITY_COLUMNS, rows)


def _csv(header: tuple[str, ...], rows) -> bytes:
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return text.getvalue().encode("utf-8-sig")


def _time(value: int, market: str, time_unit: str) -> str:
    """A day as an ISO 8601 date; an epoch second as an ISO 8601 date-time in the market's time zone."""
    if time_unit == "day":
        return _iso(value)
    return datetime.fromtimestamp(value, ZoneInfo(MARKETS[market].time_zone)).isoformat()


def _cell(value: float) -> str:
    return "" if value is None or not math.isfinite(value) else repr(float(value))
