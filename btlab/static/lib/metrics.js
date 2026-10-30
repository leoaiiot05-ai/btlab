// The metrics of §12 as text: percentages and money signed, so a value reads without its colour (SPEC.md §20.3).

import {current} from "./i18n.js";

export const METRICS = [
  ["total_return", "percent"], ["cagr", "percent"], ["volatility", "percent"], ["sharpe", "ratio"], ["sortino", "ratio"],
  ["max_drawdown", "drawdown"], ["max_drawdown_days", "count"], ["calmar", "ratio"], ["win_rate", "percent"],
  ["payoff_ratio", "ratio"], ["profit_factor", "ratio"], ["expectancy", "money"], ["expectancy_pct", "percent"],
  ["trades", "count"], ["open_trades", "count"], ["exposure", "percent"], ["time_in_market", "percent"],
  ["turnover", "ratio"], ["alpha", "percent"], ["beta", "ratio"], ["correlation", "ratio"],
];
export const KINDS = Object.fromEntries(METRICS);

export function format(kind, value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  // The maximum drawdown is stored as a positive share of the peak; it reads as the loss it is.
  if (kind === "drawdown") return format("percent", -value);
  const options = {
    percent: {style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero"},
    money: {minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero"},
    ratio: {minimumFractionDigits: 2, maximumFractionDigits: 2},
    count: {maximumFractionDigits: 0},
  }[kind] || {maximumFractionDigits: 4};
  return new Intl.NumberFormat(current(), options).format(value);
}

export function tone(value) {
  // The class of a signed value: up and down also differ by their sign, not only their colour.
  return value > 0 ? "up" : value < 0 ? "down" : "";
}

export function toneOf(kind, value) {
  // Only returns and money gain or lose; a ratio or a count has no colour.
  if (value === null || value === undefined) return "";
  if (kind === "drawdown") return tone(-value);
  return kind === "percent" || kind === "money" ? tone(value) : "";
}
