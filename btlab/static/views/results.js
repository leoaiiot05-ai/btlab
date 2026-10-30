// Results: the list, and a result's page with its flags, metrics, charts, trades and actions (SPEC.md §20.2 item 5).

import {get, patch, remove} from "../lib/api.js";
import {base, box, line, mount, points, ranges} from "../lib/charts.js";
import {BENCHMARK, DOWN, STRATEGY, UP} from "../lib/colors.js";
import {button, checkbox, clear, h, select} from "../lib/dom.js";
import {code, current, day, errorText, moment, t} from "../lib/i18n.js";
import {KINDS, METRICS, format, tone, toneOf} from "../lib/metrics.js";
import {TRADES_PAGE, stored} from "../lib/offline.js";

const SHOWN_IN_LIST = ["total_return", "cagr", "sharpe", "max_drawdown", "trades"];
const COMPARED = 4;

export function render(root, context) {
  if (context.arg) return page(root, context, Number(context.arg));
  return list(root, context);
}

// ----- the list -----

async function list(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  const chosen = new Set(context.compareSeed || []);
  context.compareSeed = null;
  const market = select([["", t("results.all_markets")], ["US", code("market", "US")], ["TW", code("market", "TW")]], "");
  const name = h("input", {type: "search", placeholder: t("results.filter_name")});
  const compare = button(t("results.compare"), () => context.go(`compare/${[...chosen].join(",")}`), "primary");
  const body = h("tbody");
  root.append(h("h2", {text: t("view.results")}),
    h("div", {class: "actions"}, market, name, context.offline ? null : compare), message,
    h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, ["", t("results.name"), t("results.created"), t("results.market"), t("results.strategy"),
        t("results.period"), ...SHOWN_IN_LIST.map((key) => t(`metric.${key}`)), t("results.flags")].map((label) => h("th", {text: label})))),
      body)));
  let rows = [];
  const draw = () => {
    clear(body);
    const text = name.value.trim().toLowerCase();
    for (const row of rows.filter((item) => (!market.value || item.market === market.value) && item.name.toLowerCase().includes(text))) {
      body.append(h("tr", {},
        h("td", {}, context.offline ? null : checkbox(chosen.has(row.id), (on) => {
          if (on) chosen.add(row.id); else chosen.delete(row.id);
          compare.disabled = chosen.size < 2 || chosen.size > COMPARED;
        })),
        h("td", {}, h("a", {href: `#results/${row.id}`, text: row.name}), row.keep_offline ? h("span", {class: "detail", text: ` · ${t("results.kept")}`}) : null),
        h("td", {class: "nowrap", text: moment(row.created_at)}),
        h("td", {text: `${code("market", row.market)} · ${code("timeframe", row.timeframe)}`}),
        h("td", {text: `${code("strategy_source", row.strategy.source)} ${row.strategy.id || ""}`}),
        h("td", {class: "nowrap", text: `${row.period.start} – ${row.period.end || t("results.latest")}`}),
        SHOWN_IN_LIST.map((key) => h("td", {class: `number ${toneOf(KINDS[key], row.metrics[key])}`,
          text: format(KINDS[key], row.metrics[key])})),
        h("td", {text: row.flags.filter((flag) => flag !== "SURVIVORSHIP").map((flag) => code("flag", flag)).join(" · ") || "—"})));
    }
    compare.disabled = chosen.size < 2 || chosen.size > COMPARED;
  };
  market.addEventListener("change", draw);
  name.addEventListener("input", draw);
  try {
    // Offline the list holds the stored results only (§20.5).
    rows = context.offline ? (await stored()).map(summary) : await get("/api/backtests?limit=1000");
    draw();
  } catch (error) {
    message.textContent = errorText(error);
  }
}

function summary(detail) {
  // A stored detail in the shape of a row of GET /api/backtests.
  const config = detail.config;
  return {...detail, strategy: config.strategy, period: {start: config.start, end: config.end}, metrics: detail.metrics.strategy,
    flags: Object.keys(detail.flags)};
}

// ----- a result -----

async function page(root, context, id) {
  const message = h("p", {class: "message", role: "alert"});
  root.append(h("p", {}, h("a", {href: "#results", text: t("results.back")})), message);
  let detail;
  let series;
  try {
    [detail, series] = await Promise.all([get(`/api/backtests/${id}`), get(`/api/backtests/${id}/series`)]);
  } catch (error) {
    message.textContent = errorText(error);
    return;
  }
  // Opening a result refreshes its stored copy, so offline it reads as last seen online.
  context.sync(id);
  const config = detail.config;
  root.append(
    h("h2", {text: detail.name}),
    h("p", {class: "detail", text: `${code("market", detail.market)} · ${code("timeframe", detail.timeframe)} · ${
      code("strategy_source", config.strategy.source)} ${config.strategy.id || ""} · ${config.start} – ${config.end || t("results.latest")}`}),
    flags(detail.flags, detail),
    actions(detail, context, message),
    h("h3", {text: t("results.metrics")}),
    cards(detail.metrics),
    h("h3", {text: t("results.equity")}), equityChart(series),
    h("h3", {text: t("results.drawdown")}), drawdownChart(series),
    h("h3", {text: t("results.monthly")}), monthlyChart(detail.tables.strategy.monthly),
    h("h3", {text: t("results.annual")}), annualChart(detail.tables.strategy.annual),
    h("h3", {text: t("results.exposure")}), exposureChart(series),
    // The cache holds no candles (§20.5).
    h("h3", {text: t("results.candles")}), context.offline ? h("p", {class: "quiet", text: t("results.offline_candles")}) : candles(detail),
    h("h3", {text: t("results.trades")}), trades(detail));
}

function flags(found, detail) {
  // SURVIVORSHIP is a quiet note; every other flag is a banner with its detail (§20.2 item 5).
  const parts = [];
  for (const [name, value] of Object.entries(found)) {
    if (name === "SURVIVORSHIP") {
      parts.push(h("p", {class: "quiet", text: code("flag", name)}));
      continue;
    }
    let extra = "";
    if (name === "LOOKAHEAD_SUSPECT" && value) extra = t("results.lookahead_at", {time: when(value.time, detail), symbol: value.symbol || "—"});
    if ((name === "PATH_FALLBACK" || name === "PDT") && value !== null) extra = t(`results.count_${name.toLowerCase()}`, {count: value});
    if (name === "IN_SAMPLE_SERIES" && value) extra = t("results.in_sample_names", {names: value.join(", ")});
    parts.push(h("div", {class: "flag-banner", role: "alert"}, h("strong", {text: `! ${code("flag", name)}`}), extra ? ` — ${extra}` : ""));
  }
  return h("section", {class: "flags"}, parts);
}

function when(value, detail) {
  return detail.time_unit === "day" ? day(value) : moment(new Date(value * 1000).toISOString(), detail.market);
}

function actions(detail, context, message) {
  if (context.offline) return h("p", {class: "quiet", text: t("results.offline_actions")});
  const name = h("input", {type: "text", value: detail.name});
  const change = async (body) => {
    try {
      await patch(`/api/backtests/${detail.id}`, body);
      message.textContent = t("results.saved");
      // The stored copy follows the new name, and keep offline changes what is stored.
      context.sync(detail.id);
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  return h("section", {class: "actions"},
    name, button(t("results.rename"), () => change({name: name.value})),
    h("label", {class: "switch"}, checkbox(detail.keep_offline, (on) => change({keep_offline: on})), h("span", {text: t("results.keep_offline")})),
    h("a", {class: "button", href: `/api/backtests/${detail.id}/export/trades.csv`, download: "", text: t("results.export_trades")}),
    h("a", {class: "button", href: `/api/backtests/${detail.id}/export/equity.csv`, download: "", text: t("results.export_equity")}),
    button(t("results.compare_with"), () => {
      context.compareSeed = [detail.id];
      context.go("results");
    }),
    button(t("results.delete"), async () => {
      if (!confirm(t("results.confirm_delete", {name: detail.name}))) return;
      try {
        await remove(`/api/backtests/${detail.id}`);
        context.sync();
        context.go("results");
      } catch (error) {
        message.textContent = errorText(error);
      }
    }, "danger"));
}

function cards(metrics) {
  // The strategy next to its benchmark, metric by metric.
  return h("section", {class: "metric-cards"}, METRICS.map(([key, kind]) => {
    const ours = metrics.strategy[key];
    const theirs = metrics.benchmark[key];
    return h("article", {class: "metric"},
      h("span", {class: "metric-name", text: t(`metric.${key}`)}),
      h("span", {class: `metric-value ${toneOf(kind, ours)}`, text: format(kind, ours)}),
      key in metrics.benchmark ? h("span", {class: "metric-other", text: `${t("results.benchmark")} ${format(kind, theirs)}`}) : null);
  }));
}

function percentAxis() {
  return {type: "value", scale: true, axisLabel: {formatter: (value) => format("percent", value)}};
}

function equityChart(series) {
  // The equity against the benchmark, on a linear or a log axis.
  const container = box();
  let log = false;
  const data = series.series;
  const build = () => ({
    ...base(),
    yAxis: {type: log ? "log" : "value", scale: true},
    series: [line(t("results.strategy"), points(series.t, data.EQUITY, series.time_unit), STRATEGY),
      line(t("results.benchmark"), points(series.t, data.BENCHMARK, series.time_unit), BENCHMARK)],
  });
  const chart = mount(container, build);
  const last = series.t.length ? series.t[series.t.length - 1] * (series.time_unit === "day" ? 86400000 : 1000) : 0;
  return h("div", {},
    h("label", {class: "switch"}, checkbox(false, (on) => {
      log = on;
      chart.update({yAxis: {type: log ? "log" : "value"}});
    }), h("span", {text: t("results.log_axis")})),
    ranges(chart, series.time_unit, last),
    container);
}

function drawdownChart(series) {
  const container = box("chart small");
  const values = series.series.DRAWDOWN.map((value) => (value === null ? null : -value));
  mount(container, () => ({
    ...base(),
    tooltip: {trigger: "axis", renderMode: "richText", valueFormatter: (value) => format("percent", value)},
    yAxis: percentAxis(),
    series: [line(t("results.drawdown"), points(series.t, values, series.time_unit), DOWN, {areaStyle: {opacity: 0.2}})],
  }));
  return container;
}

function exposureChart(series) {
  const container = box("chart small");
  mount(container, () => ({
    ...base(),
    tooltip: {trigger: "axis", renderMode: "richText", valueFormatter: (value) => format("percent", value)},
    yAxis: percentAxis(),
    series: [line(t("results.gross"), points(series.t, series.series.GROSS, series.time_unit), STRATEGY),
      line(t("results.net"), points(series.t, series.series.NET, series.time_unit), BENCHMARK)],
  }));
  return container;
}

function monthlyChart(monthly) {
  // Years by months; each cell holds its signed return, coloured green above 0 and red below (§20.3).
  const container = box("chart heat");
  const years = [...new Set(monthly.map((cell) => cell.year))].sort();
  const months = Array.from({length: 12}, (_, index) =>
    new Intl.DateTimeFormat(current(), {month: "short", timeZone: "UTC"}).format(new Date(Date.UTC(2000, index, 1))));
  const values = monthly.map((cell) => [cell.month - 1, years.indexOf(cell.year), cell.return]);
  const extreme = Math.max(0.01, ...monthly.map((cell) => Math.abs(cell.return || 0)));
  // A phone's cells are narrow: their labels keep the sign and drop the percent mark.
  const compact = (value) => `${value > 0 ? "+" : ""}${(value * 100).toFixed(1)}`;
  mount(container, () => ({
    backgroundColor: "transparent",
    animation: false,
    tooltip: {renderMode: "richText", formatter: (item) => `${years[item.value[1]]} ${months[item.value[0]]}: ${format("percent", item.value[2])}`},
    grid: {left: 52, right: 12, top: 8, bottom: 56},
    xAxis: {type: "category", data: months},
    yAxis: {type: "category", data: years},
    visualMap: {min: -extreme, max: extreme, calculable: false, orient: "horizontal", left: "center", bottom: 0,
      inRange: {color: [DOWN, "#e2e8f0", UP]}, formatter: (value) => format("percent", value)},
    series: [{type: "heatmap", data: values, label: {show: true, fontSize: container.clientWidth < 600 ? 8 : 10,
      formatter: (item) => (container.clientWidth < 600 ? compact(item.value[2]) : format("percent", item.value[2]))}}],
  }));
  return container;
}

function annualChart(annual) {
  const container = box("chart small");
  mount(container, () => ({
    backgroundColor: "transparent",
    animation: false,
    tooltip: {trigger: "axis", renderMode: "richText", valueFormatter: (value) => format("percent", value)},
    grid: {left: 60, right: 12, top: 16, bottom: 28},
    xAxis: {type: "category", data: annual.map((row) => String(row.year))},
    yAxis: percentAxis(),
    series: [{type: "bar", data: annual.map((row) => ({value: row.return, itemStyle: {color: row.return >= 0 ? UP : DOWN}})),
      label: {show: annual.length <= 30, position: "top", fontSize: 10, formatter: (item) => format("percent", item.value)}}],
  }));
  return container;
}

function candles(detail) {
  // A symbol's bars on the run's timeframe, with the trades as markers; windows of at most 5,000 bars, "earlier"
  // loading the one before.
  const container = box("chart tall");
  const message = h("p", {class: "message", role: "alert"});
  let loaded = null;
  let chart = null;
  const symbol = select(detail.config.symbols.map((value) => [value, value]), detail.config.symbols[0], () => {
    loaded = null;
    show();
  });
  const show = async (to) => {
    try {
      const query = to === undefined ? "" : `?to=${to}`;
      const found = await get(`/api/backtests/${detail.id}/candles/${encodeURIComponent(symbol.value)}${query}`);
      loaded = loaded && to !== undefined ? merge(found, loaded) : found;
      const build = () => candleOption(loaded);
      if (chart) chart.update(build());
      else chart = mount(container, build);
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  show();
  return h("div", {}, h("div", {class: "actions"}, symbol,
    button(t("results.earlier"), () => loaded && loaded.t.length && show(loaded.t[0] - 1))), message, container);
}

function merge(earlier, later) {
  const joined = {time_unit: later.time_unit, markers: [...earlier.markers, ...later.markers]};
  for (const key of ["t", "open", "high", "low", "close", "volume"]) joined[key] = [...earlier[key], ...later[key]];
  return joined;
}

function candleOption(found) {
  // Green candles rise and red ones fall (§20.3); markers: ▲ buys, ▼ sells, with their reason in the tooltip.
  const times = found.t.map((value) => value * (found.time_unit === "day" ? 86400000 : 1000));
  const bars = times.map((moment, index) => [moment, found.open[index], found.close[index], found.low[index], found.high[index]]);
  const markers = found.markers.map((marker) => ({
    value: [marker.t * (found.time_unit === "day" ? 86400000 : 1000), marker.price],
    symbol: "triangle", symbolSize: 12, symbolRotate: marker.side === "BUY" ? 0 : 180,
    itemStyle: {color: marker.side === "BUY" ? UP : DOWN},
    name: `${marker.side === "BUY" ? "▲" : "▼"} ${code("exit", marker.reason)} ${marker.quantity}`,
  }));
  return {
    ...base(),
    tooltip: {trigger: "item", renderMode: "richText"},
    series: [
      {name: "OHLC", type: "candlestick", data: bars,
        itemStyle: {color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN}},
      {name: t("results.markers"), type: "scatter", data: markers,
        tooltip: {formatter: (item) => `${item.name} @ ${item.value[1]}`}},
    ],
  };
}

function trades(detail) {
  // The trades, a page at a time.
  const body = h("tbody");
  const status = h("span", {class: "detail"});
  let offset = 0;
  const load = async () => {
    let found;
    try {
      found = await get(`/api/backtests/${detail.id}/trades?offset=${offset}&limit=${TRADES_PAGE}`);
    } catch (error) {
      // Offline only the first page is stored.
      status.textContent = errorText(error);
      return;
    }
    clear(body);
    for (const trade of found.trades) {
      body.append(h("tr", {},
        h("td", {class: "symbol", text: trade.symbol}),
        h("td", {text: code("side", trade.side.toLowerCase())}),
        h("td", {class: "number", text: format("count", trade.quantity)}),
        h("td", {class: "nowrap", text: when(trade.entry_time, detail)}),
        h("td", {class: "number", text: format("ratio", trade.entry_price)}),
        h("td", {class: "nowrap", text: trade.exit_time === null ? "—" : when(trade.exit_time, detail)}),
        h("td", {class: "number", text: format("ratio", trade.exit_price)}),
        h("td", {class: `number ${tone(trade.pnl)}`, text: format("money", trade.pnl)}),
        h("td", {class: `number ${tone(trade.return_pct)}`, text: format("percent", trade.return_pct / 100)}),
        h("td", {class: "number", text: format("money", trade.costs)}),
        h("td", {text: code("exit", trade.exit_reason)})));
    }
    status.textContent = t("results.page", {first: found.total ? offset + 1 : 0, last: offset + found.trades.length, total: found.total});
    previous.disabled = offset === 0;
    next.disabled = offset + found.trades.length >= found.total;
  };
  const previous = button(t("results.previous"), () => { offset = Math.max(0, offset - TRADES_PAGE); load(); });
  const next = button(t("results.next"), () => { offset += TRADES_PAGE; load(); });
  load();
  const headings = ["symbol", "side", "quantity", "entry_time", "entry_price", "exit_time", "exit_price", "pnl", "return", "costs", "exit_reason"];
  return h("div", {},
    h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, headings.map((key) => h("th", {text: t(`results.trade_${key}`)})))), body)),
    h("div", {class: "actions"}, previous, status, next));
}
