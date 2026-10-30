"""One backtest: configuration, data, the engine, the benchmark, metrics and the result (SPEC.md §10.1, §10.9, §10.11,
§12, §17.5).

load_run reads the stores; run_backtest is pure given the loaded data, so a pool worker can run it.
"""

import csv
import io
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from btlab.builtins import BUILTINS, BuiltinStrategy, Daily, View
from btlab.calendar import session
from btlab.costs import CostModel
from btlab.engine import EngineResult, OrderStrategy, Settings, Targets, Timeline, daily_timeline, simulate
from btlab.exits import DailyMinutePaths, ExitRules, Overlays
from btlab.features import SessionBars, session_bars
from btlab.indicators import TRADING_DAYS_PER_YEAR, Inputs
from btlab.intraday import (
    TIMEFRAME_MINUTES,
    WARMUP_SESSIONS,
    MinuteRun,
    minute_timeline,
    pdt_windows,
    session_end_bars,
)
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
from btlab.params import validate_params
from btlab.plugins import Plugin, PluginStrategy
from btlab.prices import Aligned, align
from btlab.records import ExternalSeries, MinuteBars, ShortSuspension, Trade, to_date, to_day
from btlab.rules import RuleStrategy, operands_of, validate_rule
from btlab.rules import schema as rule_schema
from btlab.sizing import SIZING_MODES, Sizing
from btlab.store import MinuteStore, Store
from btlab.taxes import TaxTable

# Daily data load from this many bars before the start, for the indicators' warm-up (§10.1).
WARMUP_BARS = 300
TIMEFRAMES = ("1d", *TIMEFRAME_MINUTES)
CLOSE_OUTS = ("NONE", "SESSION_END")
STRATEGY_SOURCES = ("builtin", "plugin", "rule")
# The flags a result can carry (§10.11); the UI localizes each.
FLAGS = ("SURVIVORSHIP", "LOOKAHEAD_SUSPECT", "IN_SAMPLE_SERIES", "SHORTS_IGNORED", "PATH_FALLBACK", "DATA_ENDED",
         "BENCHMARK_PARTIAL", "PDT")
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
    """A run that cannot proceed, with its code: NO_DATA, or NO_MINUTE_DATA with the symbols that lack minute bars."""

    def __init__(self, code: str, message: str, symbols: tuple[str, ...] = ()) -> None:
        self.code = code
        self.symbols = symbols
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
    intraday: dict | None = None


@dataclass(frozen=True, eq=False)
class RunData:
    """What a run reads, loaded from the stores. aligned holds the run's symbols from the warm-up on, start is the first
    bar of trading, rf the DTB3 rate in force on each day (§12), categories and borrow_fee_pct each symbol's settings
    (§7, None for the run's default). minutes and covered serve minute timelines, the MINUTE intrabar path and
    features (§8.2). external holds the stored spylead series by name (§5.6)."""

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
    external: Mapping[str, ExternalSeries] = field(default_factory=dict)


@dataclass(frozen=True, eq=False)
class BacktestResult:
    """A run's result (§10.11): the series of §18, the trades, metrics and tables of the strategy and its benchmark,
    the flags with their details, the data stamp, and the strategy's curve with each point's day and DTB3 rate."""

    config: dict
    time_unit: str
    series: dict[str, np.ndarray]
    trades: tuple[Trade, ...]
    metrics: dict
    tables: dict
    flags: dict
    data_stamp: dict
    curve: Curve


StrategyFactory = Callable[[View], "Targets | OrderStrategy"]


# ----- the configuration (§10.1) -----


def validate_config(raw: Any, plugins: Mapping[str, Plugin] | None = None, series: Iterable[str] = ()) -> RunConfig:
    """Checks every field and fills in the market's defaults; raises RunConfigError listing every invalid field.
    plugins are the loaded plugins a plugin strategy may name, series the stored external series a rule may read."""
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
    intraday = _intraday(timeframe, raw.get("intraday"), errors)
    strategy = _strategy(raw.get("strategy"), errors, market, symbols, timeframe, plugins or {},
                         raw.get("allow_short") is True, series)
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
        "sizing": sizing[1], "exits": exits[1], **values, "costs": costs[1], "intraday": intraday,
    }
    commission, slippage, borrow, withholding, short_fee = costs[0]
    return RunConfig(
        document=document, name=name, market=market, symbols=symbols, start=start, end=end, timeframe=timeframe,
        strategy=strategy, sizing=sizing[0], exits=exits[0], commission=commission, slippage_bps=slippage,
        borrow_fee_pct=borrow, dividend_withholding_pct=withholding, short_fee_pct=short_fee, intraday=intraday,
        **values,
    )


def _intraday(timeframe: str | None, value: Any, errors: list[tuple[str, str]]) -> dict | None:
    """Null on the daily timeline; close-out settings on minute timelines (§11.4)."""
    if timeframe == "1d" or timeframe is None:
        if value is not None:
            errors.append(("intraday", "must be null on the daily timeline"))
        return None
    fields = _Object(value or {}, "intraday", errors, {"close_out", "close_out_minutes"})
    return {
        "close_out": fields.get("close_out", _choice(*CLOSE_OUTS), "NONE"),
        "close_out_minutes": fields.get("close_out_minutes", _integer(0, 120), 0),
    }


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


def _strategy(value: Any, errors: list[tuple[str, str]], market: str, symbols: tuple[str, ...], timeframe: str | None,
              plugins: Mapping[str, Plugin], allow_short: bool, series: Iterable[str]) -> dict:
    """The source, its id and its parameters, checked against the strategy's schema (§9.2) and stored complete."""
    if value is None:
        errors.append(("strategy", "is required"))
        return {}
    rule = isinstance(value, dict) and value.get("source") == "rule"
    fields = _Object(value, "strategy", errors, {"source", "id", "params", *(("rule",) if rule else ())})
    source = fields.get("source", _choice(*STRATEGY_SOURCES))
    if source == "rule":
        return _rule(fields, errors, timeframe, allow_short, series)
    strategy_id = fields.get("id", _text)
    params = fields.get("params", _object, {})
    if strategy_id is None or params is None:
        return {"source": source, "id": strategy_id, "params": params}
    if source == "builtin":
        builtin = BUILTINS.get(strategy_id)
        if builtin is None:
            errors.append(("strategy.id", "is not a built-in strategy"))
            return {"source": source, "id": strategy_id, "params": params}
        params, found = validate_params(builtin.schema(market, symbols), params, "strategy.params")
        errors.extend(found or [(f"strategy.params.{name}", message) for name, message in builtin.check(params)])
    elif source == "plugin":
        plugin = plugins.get(strategy_id)
        if plugin is None:
            errors.append(("strategy.id", "is not a loaded plugin"))
            return {"source": source, "id": strategy_id, "params": params}
        params, found = validate_params(plugin.params, params, "strategy.params")
        errors.extend(found)
        if timeframe is not None and plugin.timeframe != timeframe:
            errors.append(("timeframe", f"must be {plugin.timeframe}, the plugin's timeframe"))
    return {"source": source, "id": strategy_id, "params": params}


def _rule(fields: _Object, errors: list[tuple[str, str]], timeframe: str | None, allow_short: bool,
          series: Iterable[str]) -> dict:
    """A rule run carries the complete rule (§10.1); its own errors keep their codes (§9.6) under strategy.rule. id is
    the stored rule's name, when there is one."""
    rule = fields.get("rule", _object)
    document = {"source": "rule", "id": fields.get("id", _optional(_text), None), "rule": rule,
                "params": fields.get("params", _object, {})}
    if rule is None or document["params"] is None:
        return document
    issues = validate_rule(rule, series)
    errors.extend((f"strategy.rule.{issue.path}" if issue.path else "strategy.rule", f"{issue.code}: {issue.message}")
                  for issue in issues)
    if issues:
        return document
    document["params"], found = validate_params(rule_schema(rule), document["params"], "strategy.params")
    errors.extend(found)
    if timeframe is not None and rule["timeframe"] != timeframe:
        errors.append(("timeframe", f"must be {rule['timeframe']}, the rule's timeframe"))
    if rule["side"] == "short" and not allow_short:
        errors.append(("allow_short", "must be true for a short rule"))
    return document


def strategy_factory(config: RunConfig, plugins: Mapping[str, Plugin] | None = None) -> StrategyFactory:
    """The strategy run_backtest takes, for a validated configuration."""
    strategy = config.strategy
    if strategy["source"] == "plugin":
        return PluginStrategy(plugins[strategy["id"]], strategy["params"])
    if strategy["source"] == "rule":
        return RuleStrategy(strategy["rule"], strategy["params"])
    return BuiltinStrategy(strategy["id"], strategy["params"])


def view(data: RunData, minute_bars: tuple[SessionBars, ...] | None = None) -> View:
    """What a strategy reads: the adjusted bars from the warm-up on (§10.2), with each symbol's minute bars by day for
    a strategy that reads features (§8.2)."""
    aligned = data.aligned
    return View(aligned.symbols, aligned.days, _adjusted(aligned), aligned.tradable, data.rf, data.start,
                minute_bars=minute_bars, external=data.external)


def _adjusted(aligned: Aligned) -> Inputs:
    return Inputs(aligned.adj_open, aligned.adj_high, aligned.adj_low, aligned.adj_close, aligned.adj_volume)


def _reads_features(config: RunConfig) -> bool:
    strategy = config.strategy
    return strategy["source"] == "rule" and any("feature" in operand for operand in operands_of(strategy["rule"]))


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
    minute_run = config.timeframe != "1d"
    if (minute_run or config.intrabar_path == "MINUTE" or _reads_features(config)) and minute_store is not None:
        covered = tuple(
            np.array([c.day for c in minute_store.minute_coverage(market, s, int(days[0]), int(days[-1])) if c.status == "OK"],
                     dtype=np.int64)
            for s in config.symbols
        )
        first_day = _first_minute_day(covered, config.start) if minute_run else int(days[0])
        first_t, last_t = session(market, first_day)[0], session(market, int(days[-1]))[1]
        data["minutes"] = tuple(minute_store.minute_bars(market, s, first_t, last_t) for s in config.symbols)
        data["covered"] = covered
    if minute_run:
        _require_minutes(config, data.get("covered"), int(days[-1]))
    return RunData(**data, external=stored_series(store))


def stored_series(store: Store) -> dict[str, ExternalSeries]:
    """The latest spylead snapshot's series by name (§5.6); none before the first snapshot."""
    snapshot = store.external_snapshot()
    return {} if snapshot is None else {found.name: found for found in snapshot.series}


def _first_minute_day(covered: tuple[np.ndarray, ...], start: int) -> int:
    """The first of the 5 covered sessions before the start, for the minute warm-up (§10.1)."""
    days = np.unique(np.concatenate(covered)) if covered else np.zeros(0, dtype=np.int64)
    first = max(0, int(np.searchsorted(days, start)) - WARMUP_SESSIONS)
    return int(days[first]) if len(days) else start


def _require_minutes(config: RunConfig, covered: tuple[np.ndarray, ...] | None, end: int) -> None:
    """A minute run needs every symbol to have coverage OK in its period (§11.2)."""
    missing = tuple(
        symbol for j, symbol in enumerate(config.symbols)
        if covered is None or not ((covered[j] >= config.start) & (covered[j] <= end)).any()
    )
    if missing:
        raise RunError("NO_MINUTE_DATA", f"no minute bars in the period for {', '.join(missing)}", missing)


def _category(infos: dict, market: str, symbol: str) -> str:
    return infos[symbol].category if symbol in infos else category_by_rule(market, symbol)


# ----- running (pure) -----


def run_backtest(config: RunConfig, data: RunData, taxes: TaxTable | None, strategy: StrategyFactory,
                 check_lookahead: bool = True, flatten: bool = False) -> BacktestResult:
    """The engine on the run's timeline, then the benchmark as BUY_HOLD of its symbol at 100% through the same engine
    (§10.9), the metrics of §12 on the daily bars or the session ends (§11.7), the flags and the result. An
    optimization's grid skips the look-ahead check (§9.7); flatten closes every position at the last bar's close,
    with costs, as a walk-forward test window does (§13.5)."""
    costs = _cost_model(config, data.categories, data.borrow_fee_pct, taxes)
    settings = _settings(config, costs, data.suspensions)
    timeline, strategy_view, minute, points = _prepare(config, data)
    paths = _minute_paths(config, data) if minute is None else minute.paths(adjusted=config.price_mode == "ADJUSTED")
    # A vectorized plugin is checked for look-ahead first; the backtest runs either way (§9.7).
    suspect = _lookahead(strategy, strategy_view) if check_lookahead else None
    if flatten:
        timeline = _flattened(timeline)
    result = simulate(timeline, strategy(strategy_view), settings, Overlays(config.exits, paths))
    rows = points - timeline.start
    days = timeline.day[points]
    rf = data.rf[np.searchsorted(data.aligned.days, days)]
    benchmark, partial = _benchmark(config, data, taxes)
    benchmark_rows = np.searchsorted(data.aligned.days, days) - data.start
    benchmark_equity = benchmark.equity_or_nan[benchmark_rows]
    curve = Curve(days, result.equity[rows], rf)
    benchmark_curve = Curve(days, benchmark_equity, rf)
    # F and COMPOSITE are fitted over their whole history: a strategy that read them is optimistic (§5.6).
    in_sample = sorted(name for name in strategy_view.read if data.external[name].in_sample)
    flags = {"SURVIVORSHIP": None, **result.flags, **({"BENCHMARK_PARTIAL": None} if partial else {}),
             **({"LOOKAHEAD_SUSPECT": suspect} if suspect is not None else {}),
             **({"IN_SAMPLE_SERIES": in_sample} if in_sample else {})}
    if minute is not None and config.market == "US":
        windows = pdt_windows(result.trades, timeline.time, timeline.session, minute.session_days, curve.equity,
                              int(timeline.session[timeline.start]))
        flags |= {"PDT": windows} if windows else {}
    known = benchmark_rows[np.isfinite(benchmark_equity)]
    return BacktestResult(
        config=config.document,
        time_unit="day" if minute is None else "second",
        series={
            "TIME": timeline.time[points].astype(np.float64), "EQUITY": curve.equity, "BENCHMARK": benchmark_equity,
            "CASH": result.cash[rows], "GROSS": result.gross[rows], "NET": result.net[rows],
            "POSITIONS": result.positions[rows].astype(np.float64),
        },
        trades=result.trades,
        metrics={
            "strategy": _metrics(curve, result, rows) | relative_metrics(curve, benchmark_curve),
            "benchmark": _metrics(benchmark_curve.since_first_value(), benchmark.result, known),
        },
        tables={"strategy": _tables(curve), "benchmark": _tables(benchmark_curve.since_first_value())},
        flags=flags,
        data_stamp=data.data_stamp,
        curve=curve,
    )


def _prepare(config: RunConfig, data: RunData) -> tuple[Timeline, View, MinuteRun | None, np.ndarray]:
    """The run's timeline, what its strategy reads, the minute run behind a minute timeline, and the bars of its curve:
    every bar from the start on the daily timeline, each session's last on minute timelines (§11.7)."""
    if config.timeframe == "1d":
        minute_bars = tuple(_session_bars(config, data, adjusted=True)) if _reads_features(config) else None
        timeline = daily_timeline(data.aligned, data.start)
        return timeline, view(data, minute_bars), None, np.arange(data.start, len(timeline.time))
    intraday = config.intraday
    minute = minute_timeline(config.market, config.timeframe, data.aligned, data.minutes, data.covered, config.start,
                             intraday["close_out"], intraday["close_out_minutes"])
    timeline = minute.timeline
    return timeline, minute_view(minute, data), minute, session_end_bars(timeline.session, timeline.start)


def lookahead(config: RunConfig, data: RunData, strategy: StrategyFactory) -> dict | None:
    """LOOKAHEAD_SUSPECT's detail for a vectorized plugin on the run's data, or None (§9.7)."""
    return _lookahead(strategy, _prepare(config, data)[1]) if isinstance(strategy, PluginStrategy) else None


def _lookahead(strategy: StrategyFactory, strategy_view: View) -> dict | None:
    return strategy.lookahead(strategy_view) if isinstance(strategy, PluginStrategy) else None


def _flattened(timeline: Timeline) -> Timeline:
    """The engine's close-out on the last bar closes every position at its close, with costs."""
    marks = np.zeros(len(timeline.time), dtype=np.int8) if timeline.closeout is None else timeline.closeout.copy()
    marks[-1] = max(marks[-1], 1)
    return replace(timeline, closeout=marks)


def window(config: RunConfig, data: RunData, first: int, stop: int,
           capital: float | None = None) -> tuple[RunConfig, RunData]:
    """A run of the loaded rows [first, stop) (§13.4): the 300 rows before first are its warm-up, as in any backtest,
    and equity starts on row first at the initial capital, or at capital. A minute run needs minute coverage for
    every symbol in the window (§11.2)."""
    days = data.aligned.days
    start, end = int(days[first]), int(days[stop - 1])
    capital = config.initial_capital if capital is None else capital
    document = {**config.document, "start": _iso(start), "end": _iso(end), "initial_capital": capital}
    windowed = replace(config, document=document, start=start, end=end, initial_capital=capital)
    if config.timeframe != "1d":
        _require_minutes(windowed, data.covered, end)
    begin = max(0, first - WARMUP_BARS)
    part = replace(data, aligned=_rows(data.aligned, begin, stop), benchmark=_rows(data.benchmark, begin, stop),
                   rf=data.rf[begin:stop], start=first - begin)
    return windowed, part


def _rows(aligned: Aligned, begin: int, stop: int) -> Aligned:
    return replace(aligned, **{item.name: value[begin:stop] for item in fields(aligned)
                               if isinstance(value := getattr(aligned, item.name), np.ndarray)})


def minute_view(minute: MinuteRun, data: RunData) -> View:
    """What a strategy reads on a minute timeline: the adjusted bars with their sessions, real-bar marks and minutes
    since the open (§8.3), VOLATILITY's P of 252 × bars per session (§8.1), and the run's daily bars (§8.4)."""
    timeline = minute.timeline
    bars = Inputs(timeline.adj_open, timeline.adj_high, timeline.adj_low, timeline.adj_close, minute.volume,
                  periods_per_year=TRADING_DAYS_PER_YEAR * minute.bars_per_session, session=timeline.session,
                  real=minute.real, minutes=minute.minutes)
    rf = data.rf[np.searchsorted(data.aligned.days, timeline.day)]
    return View(timeline.symbols, timeline.day, bars, timeline.tradable, rf, timeline.start, times=timeline.time,
                session=timeline.session, daily=Daily(data.aligned.days, _adjusted(data.aligned)), external=data.external)


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
    return DailyMinutePaths(_session_bars(config, data, adjusted=config.price_mode == "ADJUSTED"))


def _session_bars(config: RunConfig, data: RunData, adjusted: bool) -> list[SessionBars]:
    """Each symbol's 1-minute bars by day, raw or scaled to the adjusted series (§8.2). Without a minute store nothing
    is covered: every position-day falls back to the daily path, and features are NaN."""
    aligned = data.aligned
    count = len(aligned.symbols)
    nothing = MinuteBars(np.zeros(0, dtype=np.int64), *(np.zeros(0) for _ in range(5)))
    minutes = data.minutes or (nothing,) * count
    covered = data.covered or (np.zeros(0, dtype=np.int64),) * count
    return [
        session_bars(config.market, aligned.days, bars, days, aligned.multiplier[:, j] if adjusted else np.ones(len(aligned.days)))
        for j, (bars, days) in enumerate(zip(minutes, covered))
    ]


@dataclass(frozen=True, eq=False)
class _Benchmark:
    result: EngineResult
    equity_or_nan: np.ndarray


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
    return _Benchmark(result, equity), partial


def _metrics(curve: Curve, result: EngineResult, rows: np.ndarray) -> dict:
    """The curve's metrics, with the engine's exposure and positions at the curve's points (rows of the result)."""
    traded = sum(fill.quantity * fill.price for fill in result.fills)
    return curve_metrics(curve) | trade_metrics(result.trades) | activity_metrics(
        curve, result.gross[rows], result.positions[rows], traded
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
