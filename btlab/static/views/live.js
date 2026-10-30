// Live: one card per market with its portfolio, holdings, curve, current batch and history, and creating a portfolio
// from a daily backtest (SPEC.md §20.2 item 7, §14).

import {get, post, remove} from "../lib/api.js";
import {base, box, line, mount, points} from "../lib/charts.js";
import {BENCHMARK, RUNS, STRATEGY} from "../lib/colors.js";
import {button, checkbox, clear, field, h, select, stepper} from "../lib/dom.js";
import {code, day, errorText, moment, number, t} from "../lib/i18n.js";
import {format} from "../lib/metrics.js";

const MARKETS = ["US", "TW"];
const HISTORY = 10;
const REFRESHING = ["BATCH_PROPOSED", "BATCH_CONFIRMED", "BATCH_EXPIRED", "UPDATE_FINISHED", "JOB_FAILED"];

export async function render(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  root.append(h("h2", {text: t("view.live")}), message);
  let portfolios;
  let backtests;
  try {
    [portfolios, backtests] = await Promise.all([get("/api/live"), get("/api/backtests?limit=1000")]);
  } catch (error) {
    message.textContent = errorText(error);
    return;
  }
  const services = context.state.app.services;
  // A result page's "use for a live portfolio" or "write to ibguard" chose this backtest.
  const seed = context.liveSeed;
  context.liveSeed = null;
  if (context.state.app.mode === "trade" && !services.ibguard.configured) {
    root.append(h("p", {class: "warning", text: t("live.not_coordinated")}));
  }
  for (const market of MARKETS) {
    const portfolio = portfolios.find((found) => found.market === market && found.active);
    root.append(h("section", {class: "card live-card"}, h("h3", {text: code("market", market)}),
      portfolio ? portfolioCard(portfolio, context, message)
        : createForm(market, backtests.filter((found) => found.market === market && found.timeframe === "1d"), seed, context, message)));
  }
  if (services.ibguard.configured) root.append(ibguardPanel(backtests.filter((found) => found.timeframe === "1d"), seed));
  update(root);
}

function initial(backtests, seed) {
  return String(backtests.some((found) => found.id === seed) ? seed : backtests[0].id);
}

export function update(root) {
  // Every poll: the cutoff countdowns.
  for (const node of root.querySelectorAll("[data-cutoff]")) node.textContent = countdown(node.dataset.cutoff);
}

export function refreshOn(events, since) {
  return events.some((event) => event.id > since && REFRESHING.includes(event.kind));
}

function countdown(iso) {
  const left = Math.floor((new Date(iso) - Date.now()) / 60000);
  if (left <= 0) return t("live.cutoff_passed");
  return t("live.cutoff_left", {hours: Math.floor(left / 60), minutes: left % 60});
}

function createForm(market, backtests, seed, context, message) {
  if (!backtests.length) return h("p", {class: "quiet", text: t("live.no_daily")});
  const chosen = select(backtests.map((found) => [String(found.id), `${found.name} · ${moment(found.created_at)}`]),
    initial(backtests, seed));
  let capital = null;
  const amount = stepper(null, {min: 1, step: 1000, optional: true, onChange: (value) => { capital = value; }});
  const create = button(t("live.create"), async () => {
    try {
      await post("/api/live", capital === null ? {backtest_id: Number(chosen.value)} : {backtest_id: Number(chosen.value), capital});
      context.go("live");
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  }, "primary");
  return h("div", {}, h("p", {class: "quiet", text: t("live.no_portfolio")}),
    field(t("live.backtest"), chosen), field(t("live.capital"), amount), h("div", {class: "actions"}, create));
}

function portfolioCard(portfolio, context, message) {
  const act = (request) => async () => {
    try {
      await request();
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  return h("div", {},
    h("p", {class: "detail"},
      h("a", {href: `#results/${portfolio.backtest_id}`, text: portfolio.name}),
      ` · ${code("mode", portfolio.mode)} · ${t("live.capital")} ${number(portfolio.capital, 0)} · ${t("live.first_session")} ${day(portfolio.first_day)}`),
    h("p", {}, `${t("live.cash")} ${number(portfolio.cash, 0)} · ${t("live.equity")} ${number(portfolio.equity, 0)}`),
    holdings(portfolio),
    curve(portfolio),
    portfolio.batch ? batch(portfolio, portfolio.batch, act, context) : h("p", {class: "quiet", text: t("live.no_batch")}),
    h("div", {class: "actions"},
      button(t("live.regenerate"), act(() => post(`/api/live/${portfolio.id}/regenerate`))),
      button(t("live.stop"), () => {
        if (confirm(t("live.confirm_stop", {name: portfolio.name}))) act(() => remove(`/api/live/${portfolio.id}`))();
      }, "danger")),
    history(portfolio));
}

function holdings(portfolio) {
  if (!portfolio.holdings.length) return h("p", {class: "quiet", text: t("live.no_holdings")});
  return h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, ["symbol", "quantity", "price", "value", "weight"].map((key) => h("th", {text: t(`live.column_${key}`)})))),
    h("tbody", {}, portfolio.holdings.map((held) => h("tr", {},
      h("td", {class: "symbol", text: held.symbol}),
      h("td", {class: "number", text: format("count", held.quantity)}),
      h("td", {class: "number", text: format("ratio", held.price)}),
      h("td", {class: "number", text: format("money", held.value)}),
      h("td", {class: "number", text: format("percent", held.weight)}))))));
}

function curve(portfolio) {
  // The portfolio against the benchmark and the replayed backtest, from the session before its first (§14.6).
  const container = box("chart small");
  get(`/api/live/${portfolio.id}/curve`).then((found) => {
    mount(container, () => ({
      ...base(),
      series: [line(t("live.portfolio"), points(found.t, found.series.PORTFOLIO, "day"), STRATEGY, {connectNulls: true}),
        line(t("live.benchmark"), points(found.t, found.series.BENCHMARK, "day"), BENCHMARK, {connectNulls: true}),
        line(t("live.replay"), points(found.t, found.series.REPLAY, "day"), RUNS[2], {connectNulls: true})],
    }));
  }).catch(() => {});
  return container;
}

function batch(portfolio, current, act, context) {
  // The current batch: its lines with checkboxes while it can be confirmed, and the countdown to its cutoff (§14.4).
  const open = current.status === "PROPOSED";
  const kept = new Set(current.lines.filter((found) => found.status === "PROPOSED").map((found) => found.id));
  const rows = current.lines.map((found) => h("tr", {},
    h("td", {}, open && found.status === "PROPOSED" ? checkbox(true, (on) => { if (on) kept.add(found.id); else kept.delete(found.id); }) : null),
    h("td", {class: "symbol", text: found.symbol}),
    h("td", {text: code("action", found.action)}),
    h("td", {class: "number", text: format("count", found.quantity)}),
    h("td", {text: code("order_kind", found.kind)}),
    h("td", {class: "number", text: found.limit_price === null ? "—" : format("ratio", found.limit_price)}),
    h("td", {class: "number", text: format("ratio", found.ref_price)}),
    h("td", {text: code("line", found.status)}),
    h("td", {class: "detail", text: found.reason || (found.avg_price === null ? "" : `@ ${format("ratio", found.avg_price)}`)})));
  return h("div", {},
    h("h4", {text: t("live.batch", {session: day(current.session), status: code("batch", current.status)})}),
    open ? h("p", {class: "warning", data: {cutoff: current.cutoff}}) : null,
    h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, ["", "symbol", "action", "quantity", "kind", "limit", "reference", "status", "reason"]
        .map((key) => h("th", {text: key ? t(`live.column_${key}`) : ""})))),
      h("tbody", {}, rows))),
    open ? h("div", {class: "actions"}, button(t("live.confirm"), () => confirmDialog(portfolio, current, kept, act, context), "primary"))
      : null,
    current.lines.some((found) => ["SENT", "WORKING"].includes(found.status)) ? h("div", {class: "actions"},
      button(t("live.cancel_orders"), () => {
        if (confirm(t("live.confirm_cancel"))) act(() => post(`/api/live/batches/${current.id}/cancel`))();
      }, "danger")) : null);
}

function reducing(found, current) {
  // A sell of a long or a buy covering a short: ibguard's protection is lifted while its order works (§16.2).
  const held = current.positions_seen[found.symbol] || 0;
  return (found.action === "SELL" && held > 0) || (found.action === "BUY" && held < 0);
}

function confirmDialog(portfolio, current, kept, act, context) {
  // The orders the confirmation makes, where they go, and the positions whose ibguard protection they lift (§20.2).
  const chosen = current.lines.filter((found) => kept.has(found.id));
  const kind = context.state.app.services.ib.account_kind;
  const account = portfolio.mode === "test" ? t("live.virtual") : (kind ? code("account", kind) : "—");
  const lifted = portfolio.mode === "trade" && context.state.app.services.ibguard.configured
    ? chosen.filter((found) => reducing(found, current)).map((found) => found.symbol) : [];
  const dialog = h("dialog", {class: "dialog"},
    h("h3", {text: t("live.confirm_title", {session: day(current.session)})}),
    h("p", {class: kind === "live" ? "warning" : "", text: t("live.confirm_account", {account})}),
    lifted.length ? h("p", {class: "detail", text: t("live.exclusions", {symbols: lifted.join(", ")})}) : null,
    chosen.length ? h("ul", {}, chosen.map((found) => h("li", {
      text: `${code("action", found.action)} ${format("count", found.quantity)} ${found.symbol} · ${code("order_kind", found.kind)}${
        found.limit_price === null ? "" : ` ${format("ratio", found.limit_price)}`}`})))
      : h("p", {class: "quiet", text: t("live.confirm_none")}),
    h("div", {class: "actions"},
      button(t("live.confirm"), () => {
        dialog.close();
        act(() => post(`/api/live/batches/${current.id}/confirm`, {lines: [...kept]}))();
      }, "primary"),
      button(t("live.cancel"), () => dialog.close())));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

function history(portfolio) {
  const list = h("ul", {class: "history"});
  const shown = h("details", {}, h("summary", {text: t("live.history")}), list);
  shown.addEventListener("toggle", async () => {
    if (!shown.open || list.childElementCount) return;
    try {
      const found = await get(`/api/live/${portfolio.id}/batches?limit=${HISTORY}`);
      clear(list).append(...found.map((past) => h("li", {
        text: `${day(past.session)} · ${code("batch", past.status)} · ${t("live.lines", {count: past.lines.length})}`})));
    } catch (error) {
      list.append(h("li", {text: errorText(error)}));
    }
  });
  return shown;
}

function ibguardPanel(backtests, seed) {
  // ibguard's STK thresholds from a backtest that used the ibguard rule: the preview, then a confirmed write (§16.3).
  const shown = h("div");
  const message = h("p", {class: "message", role: "alert"});
  if (!backtests.length) return h("section", {class: "card"}, h("h3", {text: t("ibguard.title")}), h("p", {class: "quiet", text: t("live.no_daily")}));
  const chosen = select(backtests.map((found) => [String(found.id), `${found.name} · ${code("market", found.market)}`]), initial(backtests, seed));
  const show = async () => {
    clear(shown);
    message.textContent = "";
    try {
      const found = await get(`/api/ibguard/preview?backtest_id=${chosen.value}`);
      shown.append(preview(found, chosen.value, message));
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  return h("section", {class: "card"}, h("h3", {text: t("ibguard.title")}),
    field(t("ibguard.backtest"), chosen), h("div", {class: "actions"}, button(t("ibguard.preview"), show)), message, shown);
}

function preview(found, backtestId, message) {
  const pair = (pair) => (pair ? t("ibguard.pair", {tp: pair.tp_pct, sl: pair.sl_pct}) : "—");
  const write = button(t("ibguard.write"), async () => {
    if (!confirm(t("ibguard.confirm_write", {pair: pair(found.new), count: found.affected.length}))) return;
    try {
      await post("/api/ibguard/thresholds", {backtest_id: Number(backtestId), preview_token: found.token});
      message.textContent = t("ibguard.written");
    } catch (error) {
      message.textContent = errorText(error);
    }
  }, "primary");
  return h("div", {},
    h("p", {class: "detail", text: `${found.account} · ${found.account_kind ? code("account", found.account_kind) : "—"}${
      found.paused ? ` · ${t("ibguard.paused")}` : ""}`}),
    h("p", {text: `${t("ibguard.current")} ${pair(found.current)} → ${t("ibguard.new")} ${pair(found.new)}`}),
    found.affected.length ? h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, ["symbol", "side", "quantity", "ref", "price", "reason"].map((key) => h("th", {text: t(`ibguard.column_${key}`)})))),
      h("tbody", {}, found.affected.map((row) => h("tr", {},
        h("td", {class: "symbol", text: row.symbol}),
        h("td", {text: code("position_side", row.side)}),
        h("td", {class: "number", text: format("count", row.quantity)}),
        h("td", {class: "number", text: row.ref === null ? "—" : format("ratio", row.ref)}),
        h("td", {class: "number", text: row.price === null ? "—" : format("ratio", row.price)}),
        h("td", {class: "warning", text: code("guard_exit", row.reason)}))))))
      : h("p", {class: "quiet", text: t("ibguard.none_exit")}),
    h("div", {class: "actions"}, write));
}
