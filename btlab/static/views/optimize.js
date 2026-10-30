// Optimize: a base configuration, ranges from the schemas, the live count, the estimate and its confirmation,
// progress, and the saved optimizations with their table, heatmap, out-of-sample and walk-forward views
// (SPEC.md §20.2 item 6, §13).

import {get, post, remove} from "../lib/api.js";
import {base, box, line, mount, points} from "../lib/charts.js";
import {DOWN, STRATEGY, UP} from "../lib/colors.js";
import {button, checkbox, clear, field, h, select, stepper} from "../lib/dom.js";
import {code, errorText, moment, t} from "../lib/i18n.js";
import {KINDS, format, tone, toneOf} from "../lib/metrics.js";

const LIMIT = 2000;
const OBJECTIVES = ["SHARPE", "CAGR", "SORTINO", "CALMAR", "PROFIT_FACTOR"];
const OBJECTIVE_KEYS = {SHARPE: "sharpe", CAGR: "cagr", SORTINO: "sortino", CALMAR: "calmar", PROFIT_FACTOR: "profit_factor"};
const MODES = ["GRID", "OOS", "WFO"];
const COLUMNS = ["sharpe", "cagr", "sortino", "calmar", "profit_factor", "total_return", "max_drawdown", "trades"];

export function render(root, context) {
  if (context.arg) return detail(root, context, Number(context.arg));
  return overview(root, context);
}

export function update(root, context) {
  // Every poll: the progress of an optimization that runs.
  const box = root.querySelector("#optimize-job");
  const job = context.state.jobs.COMPUTE;
  if (!box) return;
  box.textContent = job && job.kind === "OPTIMIZE"
    ? `${code("phase", job.phase)}${job.total ? ` · ${job.done} / ${job.total}` : ""}` : "";
}

export function refreshOn(events, since) {
  return events.some((event) => event.id > since && event.kind === "OPTIMIZATION_SAVED");
}

// ----- a new optimization and the saved ones -----

async function overview(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  const form = h("section", {class: "optimize-form"});
  const saved = h("section");
  root.append(h("h2", {text: t("view.optimize")}), form, h("p", {id: "optimize-job", class: "detail", role: "status"}), message,
    h("h3", {text: t("optimize.saved")}), saved);
  update(root, context);
  try {
    const [strategies, backtests, optimizations] = await Promise.all([
      get("/api/strategies"), get("/api/backtests?limit=200"), get("/api/optimizations")]);
    newForm(form, {strategies, backtests, context, message, state: null});
    savedList(saved, optimizations);
  } catch (error) {
    message.textContent = errorText(error);
  }
}

function newForm(root, env) {
  const options = [["", t("optimize.choose_base")], ...(env.context.draftConfig ? [["form", t("optimize.current_form")]] : []),
    ...env.backtests.map((row) => [String(row.id), `${row.name} · ${moment(row.created_at)}`])];
  const draw = () => {
    clear(root);
    const state = env.state;
    root.append(field(t("optimize.base"), select(options, state ? state.base : "", choose)));
    if (!state) return;
    const count = h("p", {class: "detail", role: "status"});
    const recount = () => {
      const total = combinations(state.ranges);
      count.textContent = t("optimize.count", {count: total});
      count.classList.toggle("warning", total > LIMIT || total === 0);
    };
    root.append(
      h("fieldset", {class: "group"}, h("legend", {text: t("optimize.ranges")}),
        state.ranges.map((range) => rangeRow(range, recount, draw)),
        button(t("optimize.add_path"), () => {
          state.ranges.push({path: "", include: true, kind: "range", min: 1, max: 10, step: 1, values: []});
          draw();
        })),
      h("fieldset", {class: "group"}, h("legend", {text: t("optimize.settings")}),
        field(t("optimize.objective"), select(OBJECTIVES.map((value) => [value, code("objective", value)]), state.objective,
          (value) => { state.objective = value; })),
        field(t("optimize.min_trades"), stepper(state.min_trades, {min: 0, step: 5, integer: true,
          onChange: (value) => { state.min_trades = value; }})),
        field(t("optimize.mode"), select(MODES.map((value) => [value, code("opt_mode", value)]), state.mode,
          (value) => { state.mode = value; }))),
      count,
      h("ul", {class: "errors"}),
      button(t("optimize.run"), () => start(root, env, false), "primary"));
    recount();
  };
  const choose = async (value) => {
    if (!value) {
      env.state = null;
      return draw();
    }
    try {
      const config = value === "form" ? structuredClone(env.context.draftConfig) : (await get(`/api/backtests/${value}`)).config;
      env.state = {base: value, config, ranges: schemaRanges(config, env.strategies), objective: "SHARPE", min_trades: 20, mode: "GRID"};
      draw();
    } catch (error) {
      env.message.textContent = errorText(error);
    }
  };
  draw();
}

function schemaRanges(config, strategies) {
  // A range per strategy parameter, prefilled from its schema (§9.2): min, max and step, or the choices.
  const strategy = config.strategy;
  const schema = strategy.source === "builtin"
    ? (strategies.builtins.find((item) => item.id === strategy.id) || {params: {}}).params[config.market] || {}
    : strategy.source === "plugin" ? (strategies.plugins.find((item) => item.id === strategy.id) || {params: {}}).params
      : (strategy.rule && strategy.rule.params) || {};
  return Object.entries(schema).map(([name, param]) => {
    const path = `strategy.params.${name}`;
    if (param.type === "bool") return {path, include: false, kind: "values", values: [true, false], chosen: [true, false]};
    if (param.type === "choice") return {path, include: false, kind: "values", values: param.choices, chosen: [...param.choices]};
    const value = strategy.params && name in strategy.params ? strategy.params[name] : param.default;
    return {path, include: false, kind: "range", min: param.min ?? value, max: param.max ?? value, step: param.step || 1};
  });
}

function rangeRow(range, recount, draw) {
  const path = h("input", {type: "text", value: range.path, placeholder: "exits.stop_loss_pct"});
  path.addEventListener("change", () => { range.path = path.value.trim(); });
  const parts = [h("label", {class: "switch"}, checkbox(range.include, (on) => { range.include = on; recount(); }), path)];
  if (range.kind === "range") {
    for (const key of ["min", "max", "step"]) {
      parts.push(field(t(`optimize.${key}`), stepper(range[key], {step: range.step || 1, onChange: (value) => { range[key] = value; recount(); }})));
    }
  } else {
    parts.push(h("span", {class: "inline"}, range.values.map((value) => h("label", {class: "switch"},
      checkbox(range.chosen.includes(value), (on) => {
        range.chosen = on ? [...range.chosen, value] : range.chosen.filter((item) => item !== value);
        recount();
      }), h("span", {text: String(value === "" ? t("backtest.none") : value)})))));
  }
  return h("div", {class: "inline range-row"}, parts);
}

function combinations(ranges) {
  // §13.1's count, as the server counts it.
  return ranges.filter((range) => range.include).reduce((product, range) => {
    if (range.kind === "values") return product * range.chosen.length;
    if (!(range.step > 0) || range.max < range.min) return 0;
    return product * (Math.floor((range.max - range.min) / range.step + 1e-9) + 1);
  }, 1);
}

async function start(root, env, confirmed) {
  const state = env.state;
  const list = clear(root.querySelector(".errors"));
  const ranges = Object.fromEntries(state.ranges.filter((range) => range.include).map((range) => [range.path,
    range.kind === "values" ? {values: range.chosen} : {min: range.min, max: range.max, step: range.step}]));
  const body = {config: state.config, ranges, objective: state.objective, min_trades: state.min_trades, mode: state.mode};
  if (confirmed) body.confirm_long = true;
  try {
    await post("/api/optimizations", body);
    env.message.textContent = t("optimize.started");
    env.context.refresh();
  } catch (error) {
    if (error.code === "NEEDS_CONFIRMATION") {
      // A long optimization runs only once confirmed (§13.6, §20.4).
      const minutes = Math.round(error.body.estimate_seconds / 60);
      list.append(h("li", {}, t("optimize.estimate", {minutes}), " ",
        button(t("optimize.confirm"), () => start(root, env, true), "primary")));
      return;
    }
    env.message.textContent = errorText(error);
    for (const item of error.body.errors || []) list.append(h("li", {text: `${item.path}: ${item.message}`}));
  }
}

function savedList(root, optimizations) {
  if (!optimizations.length) return root.append(h("p", {class: "quiet", text: t("optimize.none_saved")}));
  root.append(h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, ["created", "name", "mode", "status", "best"].map((key) => h("th", {text: t(`optimize.column_${key}`)})))),
    h("tbody", {}, optimizations.map((row) => h("tr", {},
      h("td", {class: "nowrap", text: moment(row.created_at)}),
      h("td", {}, h("a", {href: `#optimize/${row.id}`, text: row.name})),
      h("td", {text: `${code("market", row.market)} · ${code("opt_mode", row.mode)}`}),
      h("td", {text: code("outcome", row.status)}),
      h("td", {text: row.best ? `${row.best.values.join(", ")} → ${format("ratio", row.best.objective)}` : "—"})))))));
}

// ----- one optimization -----

async function detail(root, context, id) {
  const message = h("p", {class: "message", role: "alert"});
  root.append(h("p", {}, h("a", {href: "#optimize", text: t("optimize.back")})), message);
  let row;
  try {
    row = await get(`/api/optimizations/${id}`);
  } catch (error) {
    message.textContent = errorText(error);
    return;
  }
  const request = row.config;
  const promote = async (values) => {
    try {
      await post(`/api/optimizations/${row.id}/promote`, {combo: values});
      message.textContent = t("optimize.promoted");
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  root.append(
    h("h2", {text: row.name}),
    h("p", {class: "detail", text: `${code("opt_mode", row.mode)} · ${code("outcome", row.status)} · ${
      code("objective", request.objective)} · ${t("optimize.min_trades")} ${request.min_trades} · ${moment(row.created_at)}`}),
    Object.keys(row.flags).filter((flag) => flag !== "SURVIVORSHIP").map((flag) =>
      h("div", {class: "flag-banner", role: "alert", text: `! ${code("flag", flag)}${flag === "IN_SAMPLE_SERIES" && row.flags[flag]
        ? ` — ${t("results.in_sample_names", {names: row.flags[flag].join(", ")})}` : ""}`})),
    button(t("optimize.delete"), async () => {
      if (!confirm(t("optimize.confirm_delete", {name: row.name}))) return;
      try {
        await remove(`/api/optimizations/${row.id}`);
        context.go("optimize");
      } catch (error) {
        message.textContent = errorText(error);
      }
    }, "danger"));
  const results = row.results;
  if (results.oos) root.append(h("h3", {text: t("optimize.oos")}), oos(results.oos));
  if (results.combinations) {
    if (results.paths.length >= 2) root.append(h("h3", {text: t("optimize.heatmap")}), heatmap(row, message));
    root.append(h("h3", {text: t("optimize.combinations")}), combinationTable(results, request, promote));
  }
  if (results.windows) root.append(h("h3", {text: t("optimize.windows")}), windows(results, promote), chained(results.chained));
}

function combinationTable(results, request, promote) {
  // Every combination, sortable by any column; the best is marked, and any valid one can be promoted.
  const body = h("tbody");
  let sortKey = OBJECTIVE_KEYS[request.objective];
  let descending = true;
  const value = (entry, key) => (key.startsWith("path:") ? entry.values[Number(key.slice(5))] : entry.metrics ? entry.metrics[key] : null);
  const draw = () => {
    const rows = results.combinations.map((entry, index) => ({entry, index}));
    rows.sort((a, b) => {
      const left = value(a.entry, sortKey);
      const right = value(b.entry, sortKey);
      if (left === null || left === undefined) return 1;
      if (right === null || right === undefined) return -1;
      return (left < right ? -1 : left > right ? 1 : 0) * (descending ? -1 : 1);
    });
    clear(body);
    for (const {entry, index} of rows) {
      body.append(h("tr", {class: index === results.best ? "best" : ""},
        entry.values.map((item) => h("td", {class: "number", text: String(item)})),
        COLUMNS.map((key) => h("td", {class: `number ${toneOf(KINDS[key], entry.metrics && entry.metrics[key])}`,
          text: entry.metrics ? format(KINDS[key], entry.metrics[key]) : "—"})),
        h("td", {text: entry.error || (index === results.best ? t("optimize.best") : "")}),
        h("td", {}, entry.metrics ? button(t("optimize.promote"), () => promote(entry.values)) : null)));
    }
  };
  const header = (label, key) => h("th", {}, button(label, () => {
    descending = sortKey === key ? !descending : true;
    sortKey = key;
    draw();
  }));
  draw();
  return h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, results.paths.map((path, index) => header(path.replace("strategy.params.", ""), `path:${index}`)),
      COLUMNS.map((key) => header(t(`metric.${key}`), key)), h("th"), h("th"))), body));
}

function heatmap(row, message) {
  // The objective over two paths, the others at the best combination's values (§13.3); null cells stay empty.
  const paths = row.results.paths;
  const container = box("chart heat");
  const x = select(paths.map((path) => [path, path]), paths[0], () => load());
  const y = select(paths.map((path) => [path, path]), paths[1], () => load());
  let chart = null;
  const load = async () => {
    try {
      const found = await get(`/api/optimizations/${row.id}/heatmap?x=${encodeURIComponent(x.value)}&y=${encodeURIComponent(y.value)}`);
      const cells = [];
      found.cells.forEach((cellRow, yi) => cellRow.forEach((cell, xi) => { if (cell !== null) cells.push([xi, yi, cell]); }));
      const extreme = Math.max(0.01, ...cells.map((cell) => Math.abs(cell[2])));
      const option = () => ({
        backgroundColor: "transparent",
        animation: false,
        tooltip: {renderMode: "richText", formatter: (item) =>
          `${found.x.path} ${found.x.values[item.value[0]]}, ${found.y.path} ${found.y.values[item.value[1]]}: ${format("ratio", item.value[2])}`},
        grid: {left: 64, right: 64, top: 24, bottom: 48},
        xAxis: {type: "category", name: found.x.path, nameLocation: "middle", nameGap: 28, data: found.x.values.map(String)},
        yAxis: {type: "category", name: found.y.path, data: found.y.values.map(String)},
        visualMap: {min: -extreme, max: extreme, orient: "vertical", right: 0, top: "middle", inRange: {color: [DOWN, "#e2e8f0", UP]}},
        series: [{type: "heatmap", data: cells, label: {show: true, fontSize: 10, formatter: (item) => format("ratio", item.value[2])}}],
      });
      if (chart) chart.update(option());
      else chart = mount(container, option);
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  load();
  return h("div", {}, h("div", {class: "actions"}, field(t("optimize.x"), x), field(t("optimize.y"), y)), container);
}

function oos(report) {
  // The best combination's in-sample metrics next to its out-of-sample ones (§13.4).
  const rows = ["total_return", "cagr", "sharpe", "sortino", "max_drawdown", "trades", "win_rate", "profit_factor"];
  return h("div", {},
    h("p", {class: "detail", text: t("optimize.split", {day: report.split})}),
    h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, ["", t("optimize.in_sample"), t("optimize.out_of_sample")].map((label) => h("th", {text: label})))),
      h("tbody", {}, rows.map((key) => h("tr", {},
        h("th", {text: t(`metric.${key}`)}),
        [report.in_sample, report.out_of_sample].map((metrics) => h("td", {class: `number ${toneOf(KINDS[key], metrics && metrics[key])}`,
          text: metrics ? format(KINDS[key], metrics[key]) : "—"}))))))));
}

function windows(results, promote) {
  // Each walk-forward window: its periods, its choice, the training objective and the test metrics (§13.5).
  return h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, ["train", "test", "status", "values", "objective", "total_return", "sharpe", "trades", ""]
      .map((key) => h("th", {text: key ? t(`optimize.window_${key}`) : ""})))),
    h("tbody", {}, results.windows.map((window) => h("tr", {},
      h("td", {class: "nowrap", text: window.train.join(" – ")}),
      h("td", {class: "nowrap", text: window.test.join(" – ")}),
      h("td", {text: code("window", window.status)}),
      h("td", {text: window.values ? window.values.join(", ") : "—"}),
      h("td", {class: "number", text: format("ratio", window.objective)}),
      h("td", {class: `number ${tone(window.metrics.total_return)}`, text: format("percent", window.metrics.total_return)}),
      h("td", {class: "number", text: format("ratio", window.metrics.sharpe)}),
      h("td", {class: "number", text: format("count", window.metrics.trades)}),
      h("td", {}, window.values ? button(t("optimize.promote"), () => promote(window.values)) : null))))));
}

function chained(report) {
  // The test windows' curves chained into one, and its metrics.
  if (!report || !report.t.length) return h("p", {class: "quiet", text: t("optimize.no_chain")});
  const container = box();
  mount(container, () => ({...base(), series: [line(t("optimize.chained"), points(report.t, report.equity, report.time_unit), STRATEGY)]}));
  const metrics = report.metrics || {};
  return h("div", {},
    h("h3", {text: t("optimize.chained")}), container,
    h("p", {class: "detail", text: ["total_return", "cagr", "sharpe", "max_drawdown", "trades"]
      .map((key) => `${t(`metric.${key}`)} ${format(KINDS[key], metrics[key])}`).join(" · ")}));
}
