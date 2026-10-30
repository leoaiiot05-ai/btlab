"""Corporate-action normalization, adjusted series and alignment to a calendar (SPEC.md §6). Pure.

Problems are returned as ActionIssue values; the caller records the events.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np

from btlab.calendar import limit_lock
from btlab.records import (
    Action,
    CapitalReduction,
    DailyBars,
    DayEvents,
    DividendPolicy,
    DividendResult,
    ParValueChange,
    SplitPrice,
    to_date,
)

# A share ratio this close to 1 changes nothing (§6.2).
RATIO_TOLERANCE = 1e-6
# The factor of an event day, net of its share ratio, must lie in (0, MAX_NET_FACTOR] (§6.3).
MAX_NET_FACTOR = 2.0
# Order of the events of one day, for the detail of a merged action (§6.2).
_DETAIL_ORDER = ("DIV_RESULT", "STOCK_DIV", "SPLIT", "REVERSE_SPLIT", "PAR_CHANGE", "CAPITAL_REDUCTION")


@dataclass(frozen=True)
class ActionIssue:
    """A problem with a symbol's actions, recorded as an event of this kind: BAD_ACTION or DIVIDEND_SPLIT_UNKNOWN."""

    kind: str
    day: int
    message: str


@dataclass(frozen=True, eq=False)
class Aligned:
    """Bars × symbols arrays on a market's calendar (§6.4). Prices are float64 (T, N), NaN outside a symbol's data.

    multiplier is the adjusted ÷ raw scale of each day. cash and ratio are the corporate actions the engine applies at a
    day's open: 0 and 1 without one. lock is +1 locked up, −1 locked down, 0 otherwise (§6.5).
    """

    days: np.ndarray
    symbols: tuple[str, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    adj_open: np.ndarray
    adj_high: np.ndarray
    adj_low: np.ndarray
    adj_close: np.ndarray
    adj_volume: np.ndarray
    multiplier: np.ndarray
    tradable: np.ndarray
    lock: np.ndarray
    cash: np.ndarray
    ratio: np.ndarray


# ----- normalization (§6.2) -----


def normalize_us_actions(actions: Sequence[Action]) -> tuple[list[Action], list[ActionIssue]]:
    """Tiingo's actions (§5.1), checked and merged to one action per kind and day."""
    issues: list[ActionIssue] = []
    return _merge([action for action in actions if _usable(action, issues)]), issues


def normalize_tw_actions(
    results: Sequence[DividendResult],
    policies: Sequence[DividendPolicy],
    splits: Sequence[SplitPrice],
    par_changes: Sequence[ParValueChange],
    reductions: Sequence[CapitalReduction],
) -> tuple[list[Action], list[ActionIssue]]:
    """FinMind's rows of one symbol as CASH_DIV and SHARE_RATIO actions, one per kind and day (§6.2).

    On a day with a dividend result, ref_before is its P, and ref_after its A divided by the day's other share ratios:
    the reference price after all of the day's events, so the day's factor stays ref_after / ref_before (§6.3).
    """
    issues: list[ActionIssue] = []
    cash: dict[int, float] = {}
    for policy in policies:
        if policy.cash_ex_day is not None:
            cash[policy.cash_ex_day] = cash.get(policy.cash_ex_day, 0.0) + policy.cash
    dividends: list[Action] = []
    references: dict[int, tuple[float, float]] = {}
    for result in dict.fromkeys(results):
        if result.day in references:
            issues.append(ActionIssue("BAD_ACTION", result.day, f"a second dividend result on {to_date(result.day)} is skipped"))
            continue
        actions = _dividend_result(result, cash.get(result.day), issues)
        if actions:
            references[result.day] = (result.before_price, result.after_price)
            dividends += actions
    others = [
        *(_share_event(s.day, s.before_price, s.after_price, "SPLIT") for s in dict.fromkeys(splits)),
        *(_share_event(c.day, c.before_close, c.after_ref_close, "PAR_CHANGE") for c in dict.fromkeys(par_changes)),
        *(_share_event(r.day, r.closing_price, r.reference_price, "CAPITAL_REDUCTION") for r in dict.fromkeys(reductions)),
    ]
    usable_others = [action for action in others if _usable(action, issues)]
    merged = _merge([action for action in dividends if _usable(action, issues)] + usable_others)
    other_ratio: dict[int, float] = {}
    for action in usable_others:
        other_ratio[action.day] = other_ratio.get(action.day, 1.0) * action.ratio
    return [_with_references(action, references, other_ratio) for action in merged], issues


def _dividend_result(result: DividendResult, cash: float | None, issues: list[ActionIssue]) -> list[Action]:
    """A dividend result with P = before_price and A = after_price; cash is D from the matching policy rows."""
    p, a = result.before_price, result.after_price
    when = to_date(result.day)
    if not all(math.isfinite(price) and price > 0 for price in (p, a)):
        issues.append(ActionIssue("BAD_ACTION", result.day, f"the dividend result of {when} has unusable reference prices"))
        return []

    def dividend(amount: float) -> Action:
        return Action(result.day, "CASH_DIV", "DIV_RESULT", cash=amount, ref_before=p, ref_after=a)

    def shares(ratio: float) -> Action:
        return Action(result.day, "SHARE_RATIO", "STOCK_DIV", ratio=ratio, ref_before=p, ref_after=a)

    if result.kind == "息":
        return [dividend(p - a if cash is None else cash)]
    if result.kind == "權":
        return [shares(p / a)]
    if result.kind == "權息":
        if cash is not None:
            return [dividend(cash), shares((p - cash) / a)]
        issues.append(ActionIssue(
            "DIVIDEND_SPLIT_UNKNOWN", result.day,
            f"the stock and cash dividend of {when} has no policy row; its value is kept as a share ratio",
        ))
        return [shares(p / a)]
    issues.append(ActionIssue("BAD_ACTION", result.day, f"the dividend result of {when} has an unknown kind"))
    return []


def _share_event(day: int, before: float, after: float, detail: str) -> Action:
    ratio = before / after if after else math.nan
    if detail == "SPLIT" and ratio < 1:
        detail = "REVERSE_SPLIT"
    return Action(day, "SHARE_RATIO", detail, ratio=ratio)


def _usable(action: Action, issues: list[ActionIssue]) -> bool:
    """A cash amount or ratio that is not finite, a ratio ≤ 0 or a cash amount ≤ 0 is skipped with BAD_ACTION.

    A ratio within 1e-6 of 1 is dropped without one.
    """
    if action.kind == "CASH_DIV":
        good, what = math.isfinite(action.cash) and action.cash > 0, "cash dividend"
    elif action.kind == "SHARE_RATIO":
        good, what = math.isfinite(action.ratio) and action.ratio > 0, "share ratio"
    else:
        good, what = False, "action of an unknown kind"
    if not good:
        issues.append(ActionIssue("BAD_ACTION", action.day, f"the {what} of {to_date(action.day)} is not usable and is skipped"))
        return False
    return action.kind == "CASH_DIV" or abs(action.ratio - 1) > RATIO_TOLERANCE


def _merge(actions: Sequence[Action]) -> list[Action]:
    """One action per kind and day, as the store holds them: ratios multiply and cash amounts add (§6.3)."""
    merged: dict[tuple[int, str], Action] = {}
    for action in sorted(actions, key=lambda a: _DETAIL_ORDER.index(a.detail)):
        first = merged.get((action.day, action.kind))
        merged[action.day, action.kind] = action if first is None else replace(
            first, cash=first.cash + action.cash, ratio=first.ratio * action.ratio
        )
    return [
        merged[key] for key in sorted(merged)
        if key[1] == "CASH_DIV" or abs(merged[key].ratio - 1) > RATIO_TOLERANCE
    ]


def _with_references(action: Action, references: dict[int, tuple[float, float]], other_ratio: dict[int, float]) -> Action:
    if action.day not in references:
        return action
    p, a = references[action.day]
    return replace(action, ref_before=p, ref_after=a / other_ratio.get(action.day, 1.0))


# ----- factors and adjusted series (§6.3) -----


def day_events(market: str, bars: DailyBars, actions: Sequence[Action]) -> tuple[DayEvents, list[ActionIssue]]:
    """Each event day's cash, share ratio and factor g, from a symbol's bars and normalized actions (§6.3).

    US: g = (C_prev / r − D) / C_prev, with C_prev the last raw close before the day. TW: g = ref_after / ref_before on a
    dividend-result day, else 1 / r. The factor net of the share ratio, g × r, must lie in (0, 2]; otherwise its dividend
    part is ignored (g = 1 / r) and BAD_ACTION is returned. A reverse split alone has g × r = 1 (example A6).
    """
    by_day: dict[int, list[Action]] = {}
    for action in actions:
        by_day.setdefault(int(action.day), []).append(action)
    days = sorted(by_day)
    cash = np.array([sum(a.cash for a in by_day[day] if a.kind == "CASH_DIV") for day in days], dtype=np.float64)
    ratio = np.array([math.prod(a.ratio for a in by_day[day] if a.kind == "SHARE_RATIO") for day in days], dtype=np.float64)
    previous = _previous_close(bars, np.array(days, dtype=np.int64))
    factor = np.ones(len(days))
    issues: list[ActionIssue] = []
    for i, day in enumerate(days):
        net = _net_factor(market, by_day[day], cash[i], ratio[i], previous[i])
        if not 0 < net <= MAX_NET_FACTOR:
            issues.append(ActionIssue(
                "BAD_ACTION", day, f"the adjustment factor of {to_date(day)} is out of range; its dividend part is ignored"
            ))
            net = 1.0
        factor[i] = net / ratio[i]
    return DayEvents(np.array(days, dtype=np.int64), cash, ratio, factor), issues


def _net_factor(market: str, actions: list[Action], cash: float, ratio: float, previous_close: float) -> float:
    """g × r: the part of a day's factor that the share ratio does not explain."""
    if market == "TW":
        references = next(((a.ref_before, a.ref_after) for a in actions if a.ref_before is not None), None)
        return 1.0 if references is None else references[1] / references[0] * ratio
    if not math.isfinite(previous_close) or previous_close <= 0:
        return 1.0
    return 1 - ratio * cash / previous_close


def adjusted(bars: DailyBars, events: DayEvents) -> DailyBars:
    """adj_price(τ) = raw_price(τ) × Π g_t and adj_volume(τ) = raw_volume(τ) × Π r_t over the event days after τ."""
    price = _product_after(events.day, events.factor, bars.day)
    volume = _product_after(events.day, events.ratio, bars.day)
    return DailyBars(
        bars.day, bars.open * price, bars.high * price, bars.low * price, bars.close * price, bars.volume * volume,
        bars.no_trade,
    )


def _product_after(event_days: np.ndarray, values: np.ndarray, days: np.ndarray) -> np.ndarray:
    """Π values over the event days strictly after each day."""
    suffix = np.append(np.cumprod(values[::-1])[::-1], 1.0)
    return suffix[np.searchsorted(event_days, days, side="right")]


def _previous_close(bars: DailyBars, days: np.ndarray) -> np.ndarray:
    """The last finite raw close before each day; NaN without one."""
    closes = _carried_closes(bars)
    before = np.searchsorted(bars.day, days, side="left") - 1
    return np.where(before >= 0, closes[np.maximum(before, 0)], np.nan)


def _carried_closes(bars: DailyBars) -> np.ndarray:
    """Each bar's close, or the last traded close before it on a bar without one."""
    traded = _traded(bars)
    source = np.maximum.accumulate(np.where(traded, np.arange(len(bars.day)), -1)) if len(bars.day) else np.zeros(0, int)
    return np.where(source >= 0, bars.close[np.maximum(source, 0)], np.nan)


def _traded(bars: DailyBars) -> np.ndarray:
    return np.isfinite(bars.close) & ~bars.no_trade


# ----- alignment (§6.4, §6.5) -----


def align(
    market: str, days: np.ndarray, histories: Mapping[str, tuple[DailyBars, Sequence[Action]]]
) -> tuple[Aligned, list[tuple[str, ActionIssue]]]:
    """Every symbol on the market's calendar days. histories maps each symbol, in run order, to its stored bars and
    normalized actions; pass them through the latest stored day, because adjusted prices are relative to it."""
    days = np.asarray(days, dtype=np.int64)
    columns, issues = [], []
    for symbol, (bars, actions) in histories.items():
        events, found = day_events(market, bars, actions)
        issues += [(symbol, issue) for issue in found]
        columns.append(_column(days, bars, events))
    stack = {name: np.column_stack([column[name] for column in columns]) if columns else np.empty((len(days), 0))
             for name in _COLUMNS}
    tradable = stack["tradable"].astype(bool)
    lock = limit_lock(market, days, stack["open"], stack["high"], stack["low"], stack["close"], stack["ref"])
    return Aligned(
        days=days, symbols=tuple(histories), lock=np.where(tradable, lock, 0).astype(np.int8), tradable=tradable,
        **{name: stack[name] for name in _COLUMNS if name not in ("ref", "tradable")},
    ), issues


_COLUMNS = (
    "open", "high", "low", "close", "volume", "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
    "multiplier", "tradable", "cash", "ratio", "ref",
)


def _column(days: np.ndarray, bars: DailyBars, events: DayEvents) -> dict[str, np.ndarray]:
    """One symbol's arrays on the calendar days."""
    count = len(days)
    column = {name: np.full(count, np.nan) for name in _COLUMNS}
    column["tradable"] = np.zeros(count, dtype=bool)
    column["cash"], column["ratio"], day_factor = np.zeros(count), np.ones(count), np.ones(count)
    if not count or not len(bars.day):
        return column
    # An action on a day missing from the calendar applies on the next calendar day.
    slots = np.searchsorted(days, events.day, side="left")
    for slot, cash, ratio, factor, day in zip(slots, events.cash, events.ratio, events.factor, events.day):
        if day >= days[0] and slot < count:
            column["cash"][slot] += cash
            column["ratio"][slot] *= ratio
            day_factor[slot] *= factor
    adj = adjusted(bars, events)
    last = np.searchsorted(bars.day, days, side="right") - 1
    inside = (last >= 0) & (days <= bars.day[-1])
    at = np.maximum(last, 0)
    real = inside & (bars.day[at] == days) & _traded(bars)[at]
    carried = inside & ~real
    source = np.maximum.accumulate(np.where(_traded(bars), np.arange(len(bars.day)), -1))[at]
    has_source = source >= 0
    for name, raw, adjusted_values in (
        ("open", bars.open, adj.open), ("high", bars.high, adj.high), ("low", bars.low, adj.low), ("close", bars.close, adj.close)
    ):
        column[name] = np.where(real, raw[at], np.where(carried & has_source, bars.close[np.maximum(source, 0)], np.nan))
        column[f"adj_{name}"] = np.where(
            real, adjusted_values[at], np.where(carried & has_source, adj.close[np.maximum(source, 0)], np.nan)
        )
    column["volume"] = np.where(real, bars.volume[at], np.where(carried, 0.0, np.nan))
    column["adj_volume"] = np.where(real, adj.volume[at], np.where(carried, 0.0, np.nan))
    column["multiplier"] = np.where(inside, _product_after(events.day, events.factor, days), np.nan)
    prices = np.column_stack([column[name] for name in ("open", "high", "low", "close")])
    column["tradable"] = real & (column["volume"] > 0) & np.isfinite(prices).all(axis=1)
    column["ref"] = _previous_close(bars, days) * day_factor
    return column
