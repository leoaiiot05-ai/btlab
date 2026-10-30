// Backtest: the run form of §10.1 in groups, the parameter form generated from the strategy's schema, costs prefilled
// per market, and errors shown at their fields by JSON path (SPEC.md §20.2 item 3).

import {get, post} from "../lib/api.js";
import {button, checkbox, clear, field, h, select, showErrors, stepper} from "../lib/dom.js";
import {code, current, errorText, t} from "../lib/i18n.js";

const MARKETS = ["US", "TW"];
const TIMEFRAMES = ["1d", "1m", "5m", "15m", "30m", "60m"];
const SOURCES = ["builtin", "plugin", "rule"];
const SIZING = {
  FIXED_FRACTION: {fraction: {min: 0, max: 1, step: 0.05, optional: true}},
  FIXED_AMOUNT: {amount: {min: 0, step: 1000}},
  FIXED_SHARES: {shares: {min: 0, step: 1}},
  VOL_RISK: {risk_pct: {min: 0, max: 100, step: 0.25, value: 1}, atr_n: {min: 1, max: 1000, step: 1, integer: true, value: 20},
    atr_mult: {min: 0, step: 0.5, value: 2}},
};
const EXITS = {
  stop_loss_pct: {min: 0, max: 100, step: 0.5, value: 5},
  take_profit_pct: {min: 0, step: 1, value: 15},
  trailing_pct: {min: 0, max: 100, step: 0.5, value: 10},
  max_hold_bars: {min: 1, step: 1, integer: true, value: 20},
};
const PAIRS = {
  atr_stop: {n: {min: 1, max: 1000, step: 1, integer: true, value: 14}, mult: {min: 0, step: 0.5, value: 3}},
  ibguard: {tp_pct: {min: 0, max: 1000, step: 1, value: 15}, sl_pct: {min: 0, max: 100, step: 0.5, value: 5}},
};
const CHOICES = {
  price_mode: ["REALISTIC", "ADJUSTED"], fill_timing: ["NEXT_OPEN", "SAME_CLOSE"], intrabar_path: ["DAILY", "MINUTE"],
};
const YEARS_BACK = 10;

export function render(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  const form = h("form", {class: "run-form", novalidate: true});
  root.append(h("h2", {text: t("view.backtest")}), form, message);
  Promise.all([get("/api/strategies"), get("/api/lists"), get("/api/rules")]).then(([strategies, lists, rules]) => {
    const draft = blank("US", strategies.defaults.US);
    if (context.draftRule) Object.assign(draft, ruleChoice(context.draftRule, null));
    build(form, draft, {strategies, lists, rules, context, message});
  }).catch((error) => {
    message.textContent = errorText(error);
  });
}

function blank(market, defaults) {
  const start = new Date();
  start.setUTCFullYear(start.getUTCFullYear() - YEARS_BACK);
  return {
    name: "", market, symbols: [], list_id: null, start: start.toISOString().slice(0, 10), end: null, timeframe: "1d",
    strategy: {source: "builtin", id: "MA_CROSS", params: {}}, sizing: {mode: "FIXED_FRACTION"}, exits: {},
    price_mode: "REALISTIC", fill_timing: "NEXT_OPEN", intrabar_path: "DAILY", allow_short: false, board_lots_only: false,
    initial_capital: defaults.initial_capital, max_position_pct: 100, costs: structuredClone(defaults.costs),
    benchmark: defaults.benchmark, intraday: null,
  };
}

function ruleChoice(rule, saved) {
  return {timeframe: rule.timeframe, strategy: {source: "rule", id: saved ? saved.name : null, rule, params: {}}};
}

function build(form, draft, env) {
  // Structural choices (market, strategy, timeframe, sizing mode, exit toggles) rebuild the form from the draft.
  const rebuild = () => build(form, draft, env);
  clear(form);
  form.onsubmit = (event) => {
    event.preventDefault();
    submit(form, draft, env);
  };
  form.append(
    group(t("backtest.universe"), universe(draft, env, rebuild)),
    group(t("backtest.strategy"), strategy(draft, env, rebuild)),
    group(t("backtest.sizing"), sizing(draft, rebuild)),
    group(t("backtest.exits"), exits(draft, rebuild)),
    group(t("backtest.execution"), execution(draft, rebuild)),
    group(t("backtest.costs"), costs(draft)),
    h("ul", {class: "errors"}),
    h("button", {type: "submit", class: "button primary", text: t("backtest.run")}));
}

function group(title, children) {
  return h("fieldset", {class: "group"}, h("legend", {text: title}), children);
}

function universe(draft, env, rebuild) {
  const lists = env.lists.filter((found) => found.market === draft.market);
  const symbols = h("textarea", {rows: "2", autocapitalize: "characters", value: draft.symbols.join(", "),
    on: {change: (event) => {
      draft.symbols = event.target.value.split(/[\s,]+/).filter(Boolean);
      draft.list_id = null;
    }}});
  symbols.value = draft.symbols.join(", ");
  return [
    field(t("backtest.name"), text(draft.name, (value) => { draft.name = value; }), "name"),
    field(t("backtest.market"), select(MARKETS.map((name) => [name, code("market", name)]), draft.market, (value) => {
      const kept = {name: draft.name, start: draft.start, end: draft.end, strategy: draft.strategy};
      Object.assign(draft, blank(value, env.strategies.defaults[value]), kept, {strategy: {...kept.strategy, params: {}}});
      rebuild();
    }), "market"),
    field(t("backtest.list"), select([["", t("backtest.none")], ...lists.map((found) => [String(found.id), found.name])],
      draft.list_id ? String(draft.list_id) : "", (value) => {
        const found = lists.find((item) => String(item.id) === value);
        draft.list_id = found ? found.id : null;
        draft.symbols = found ? [...found.symbols] : draft.symbols;
        rebuild();
      }), "list_id"),
    field(t("backtest.symbols"), symbols, "symbols"),
    field(t("backtest.start"), date(draft.start, (value) => { draft.start = value; }), "start"),
    field(t("backtest.end"), date(draft.end, (value) => { draft.end = value || null; }), "end"),
  ];
}

function strategy(draft, env, rebuild) {
  const chosen = draft.strategy;
  const parts = [field(t("backtest.source"), select(SOURCES.map((name) => [name, code("strategy_source", name)]), chosen.source,
    (value) => {
      draft.strategy = value === "builtin" ? {source: value, id: "MA_CROSS", params: {}}
        : value === "plugin" ? {source: value, id: env.strategies.plugins[0] ? env.strategies.plugins[0].id : null, params: {}}
        : ruleChoice(env.context.draftRule || {timeframe: draft.timeframe}, null).strategy;
      rebuild();
    }), "strategy.source")];
  let schema = {};
  if (chosen.source === "builtin") {
    const found = env.strategies.builtins.find((item) => item.id === chosen.id) || env.strategies.builtins[0];
    parts.push(field(t("backtest.builtin"), select(env.strategies.builtins.map((item) => [item.id, name(item)]), found.id,
      (value) => { draft.strategy = {source: "builtin", id: value, params: {}}; rebuild(); }), "strategy.id"));
    schema = found.params[draft.market];
  } else if (chosen.source === "plugin") {
    const found = env.strategies.plugins.find((item) => item.id === chosen.id);
    parts.push(field(t("backtest.plugin"), select(env.strategies.plugins.map((item) => [item.id, name(item)]), chosen.id,
      (value) => { draft.strategy = {source: "plugin", id: value, params: {}}; rebuild(); }), "strategy.id"));
    for (const failed of env.strategies.errors) {
      parts.push(h("p", {class: "detail warning", text: t("backtest.plugin_error", {id: failed.id, error: failed.error})}));
    }
    if (found) {
      draft.timeframe = found.timeframe;
      schema = found.params;
    }
  } else {
    parts.push(ruleField(draft, env, rebuild));
    if (chosen.rule && chosen.rule.timeframe) draft.timeframe = chosen.rule.timeframe;
    schema = chosen.rule && chosen.rule.params ? chosen.rule.params : {};
  }
  parts.push(...Object.entries(schema).map(([key, param]) => paramField(key, param, draft)));
  const fixed = chosen.source !== "builtin";
  parts.push(field(t("backtest.timeframe"), select(TIMEFRAMES.map((value) => [value, code("timeframe", value)]), draft.timeframe,
    (value) => {
      draft.timeframe = value;
      draft.intraday = value === "1d" ? null : draft.intraday || {close_out: "NONE", close_out_minutes: 0};
      rebuild();
    }), "timeframe"));
  if (fixed) parts[parts.length - 1].querySelector("select").disabled = true;
  if (draft.timeframe !== "1d") {
    draft.intraday = draft.intraday || {close_out: "NONE", close_out_minutes: 0};
    parts.push(
      field(t("backtest.close_out"), select(["NONE", "SESSION_END"].map((value) => [value, code("close_out", value)]),
        draft.intraday.close_out, (value) => { draft.intraday.close_out = value; }), "intraday.close_out"),
      field(t("backtest.close_out_minutes"), stepper(draft.intraday.close_out_minutes, {min: 0, max: 120, step: 5, integer: true,
        onChange: (value) => { draft.intraday.close_out_minutes = value; }}), "intraday.close_out_minutes"));
  }
  return parts;
}

function ruleField(draft, env, rebuild) {
  const options = [["", t("backtest.rule_draft")], ...env.rules.map((saved) => [String(saved.id), saved.name])];
  const box = h("div", {class: "inline"},
    field(t("backtest.rule"), select(options, "", (value) => {
      const saved = env.rules.find((item) => String(item.id) === value);
      Object.assign(draft, ruleChoice(saved ? saved.rule : env.context.draftRule || {}, saved));
      rebuild();
    }), "strategy.rule"),
    button(t("backtest.open_rule_builder"), () => {
      env.context.draftRule = draft.strategy.rule || null;
      env.context.go("rules");
    }));
  return box;
}

function paramField(key, param, draft) {
  // One parameter of a §9.2 schema: steppers for numbers, a switch for booleans, a list for choices.
  const params = draft.strategy.params;
  if (!(key in params)) params[key] = param.default;
  const label = (current() === "zh-TW" ? param.label_zh : param.label_en) || key;
  let control;
  if (param.type === "bool") {
    control = checkbox(params[key], (value) => { params[key] = value; });
  } else if (param.type === "choice") {
    // DUAL_MOMENTUM's safe asset is one of the run's symbols, or cash (§9.3).
    const choices = key === "safe" ? ["", ...draft.symbols] : param.choices;
    control = select(choices.map((value) => [value, value || t("backtest.cash")]), params[key], (value) => { params[key] = value; });
  } else {
    control = stepper(params[key], {min: param.min, max: param.max, step: param.step || (param.type === "int" ? 1 : 0.1),
      integer: param.type === "int", onChange: (value) => { params[key] = value; }});
  }
  return field(label, control, `strategy.params.${key}`);
}

function sizing(draft, rebuild) {
  const mode = draft.sizing.mode;
  return [
    field(t("backtest.sizing_mode"), select(Object.keys(SIZING).map((value) => [value, code("sizing", value)]), mode, (value) => {
      draft.sizing = {mode: value};
      for (const [key, options] of Object.entries(SIZING[value])) if ("value" in options) draft.sizing[key] = options.value;
      rebuild();
    }), "sizing.mode"),
    ...Object.entries(SIZING[mode]).map(([key, options]) => field(t(`backtest.sizing_${key}`),
      stepper(draft.sizing[key] ?? null, {...options, onChange: (value) => { draft.sizing[key] = value; }}), `sizing.${key}`)),
  ];
}

function exits(draft, rebuild) {
  // One switch per overlay, and its fields when it is on (§10.8).
  const parts = [];
  for (const [key, options] of Object.entries(EXITS)) {
    const on = draft.exits[key] !== undefined && draft.exits[key] !== null;
    parts.push(h("div", {class: "inline"},
      h("label", {class: "switch"}, checkbox(on, (value) => {
        draft.exits[key] = value ? options.value : null;
        rebuild();
      }), h("span", {text: t(`backtest.exit_${key}`)})),
      on ? field("", stepper(draft.exits[key], {...options, onChange: (value) => { draft.exits[key] = value; }}), `exits.${key}`) : null));
  }
  for (const [key, members] of Object.entries(PAIRS)) {
    const on = Boolean(draft.exits[key]);
    parts.push(h("div", {class: "inline"},
      h("label", {class: "switch"}, checkbox(on, (value) => {
        draft.exits[key] = value ? Object.fromEntries(Object.entries(members).map(([name, options]) => [name, options.value])) : null;
        rebuild();
      }), h("span", {text: t(`backtest.exit_${key}`)})),
      on ? Object.entries(members).map(([name, options]) => field(t(`backtest.exit_${key}_${name}`),
        stepper(draft.exits[key][name], {...options, onChange: (value) => { draft.exits[key][name] = value; }}),
        `exits.${key}.${name}`)) : null));
  }
  return parts;
}

function execution(draft) {
  return [
    ...Object.entries(CHOICES).map(([key, values]) => field(t(`backtest.${key}`),
      select(values.map((value) => [value, code(key, value)]), draft[key], (value) => { draft[key] = value; }), key)),
    h("label", {class: "switch", data: {path: "allow_short"}}, checkbox(draft.allow_short, (value) => { draft.allow_short = value; }),
      h("span", {text: t("backtest.allow_short")})),
    draft.market === "TW" ? h("label", {class: "switch", data: {path: "board_lots_only"}},
      checkbox(draft.board_lots_only, (value) => { draft.board_lots_only = value; }), h("span", {text: t("backtest.board_lots_only")})) : null,
  ];
}

function costs(draft) {
  // Prefilled with the market's defaults (§4.1) and editable.
  const commission = draft.costs.commission;
  const numbers = (target, key, path, options) => field(t(`backtest.cost_${key}`),
    stepper(target[key], {...options, onChange: (value) => { target[key] = value; }}), path);
  return [
    ...Object.keys(commission).map((key) => numbers(commission, key, `costs.commission.${key}`, {min: 0, step: key === "min" ? 1 : 0.001})),
    ...Object.keys(draft.costs).filter((key) => key !== "commission")
      .map((key) => numbers(draft.costs, key, `costs.${key}`, {min: 0, step: key === "slippage_bps" ? 1 : 0.05})),
    numbers(draft, "initial_capital", "initial_capital", {min: 0, step: draft.market === "US" ? 10000 : 100000}),
    numbers(draft, "max_position_pct", "max_position_pct", {min: 0, max: 100, step: 5}),
    field(t("backtest.cost_benchmark"), text(draft.benchmark, (value) => { draft.benchmark = value; }), "benchmark"),
  ];
}

async function submit(form, draft, env) {
  const config = {...draft, symbols: draft.symbols.map((symbol) => symbol.toUpperCase())};
  if (config.strategy.source !== "rule") delete config.strategy.rule;
  try {
    await post("/api/backtests", {config});
    showErrors(form, []);
    clear(form.querySelector(".errors"));
    env.message.textContent = t("backtest.started");
    env.context.refresh();
  } catch (error) {
    const errors = error.body && error.body.errors ? error.body.errors : [{path: error.path, message: errorText(error)}];
    const left = showErrors(form, errors, "config.");
    const list = clear(form.querySelector(".errors"));
    for (const item of left) list.append(h("li", {text: item.path ? `${item.path}: ${item.message}` : item.message}));
    env.message.textContent = errorText(error);
  }
}

function name(item) {
  return current() === "zh-TW" ? item.name_zh || item.id : item.name_en || item.id;
}

function text(value, onChange) {
  const input = h("input", {type: "text", value: value || ""});
  input.addEventListener("change", () => onChange(input.value));
  return input;
}

function date(value, onChange) {
  const input = h("input", {type: "date", value: value || ""});
  input.addEventListener("change", () => onChange(input.value));
  return input;
}
