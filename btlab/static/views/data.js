// Data: lists, the symbols table and the market updates (SPEC.md §20.2 item 2, §7).

import {get, patch, post, put, remove} from "../lib/api.js";
import {append, button, checkbox, clear, field, h, select, showErrors} from "../lib/dom.js";
import {code, day, errorText, moment, number, t} from "../lib/i18n.js";

const MARKETS = ["US", "TW"];

export function render(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  const lists = h("section", {class: "lists"});
  const symbols = h("section", {class: "symbols"});
  const spylead = h("section", {class: "spylead"});
  let market = "US";
  const reload = async () => {
    try {
      const [found, rows, strategies, snapshot] = await Promise.all([
        get("/api/lists"), get(`/api/symbols?market=${market}`), get("/api/strategies"), get("/api/spylead")]);
      showLists(lists, found, reload, message);
      showSymbols(symbols, market, rows, strategies.defaults[market].categories, reload, message);
      showSpylead(spylead, snapshot, context, message);
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  root.append(
    h("h2", {text: t("view.data")}),
    h("div", {class: "actions"},
      MARKETS.map((name) => button(t("data.update", {market: code("market", name)}), () => startUpdate(name, context, message), "primary"))),
    h("div", {id: "data-job"}),
    message,
    h("h3", {text: t("data.lists")}),
    createForm(reload, message),
    lists,
    h("h3", {text: t("data.symbols")}),
    select(MARKETS.map((name) => [name, code("market", name)]), market, (value) => {
      market = value;
      reload();
    }),
    symbols,
    h("h3", {text: t("data.minute")}),
    minuteControls(context, message),
    h("div", {id: "minute-job"}),
    h("h3", {text: t("data.spylead")}),
    spylead);
  showJob(root, context);
  showMinute(root, context);
  reload();
}

export function update(root, context) {
  showJob(root, context);
  showMinute(root, context);
}

export function refreshOn(events, since) {
  // A finished update changes the symbols table.
  return events.some((event) => event.id > since && ["UPDATE_FINISHED", "UP_TO_DATE", "JOB_FAILED"].includes(event.kind));
}

function showJob(root, context) {
  const box = root.querySelector("#data-job");
  const job = context.state.jobs.DATA;
  if (!box) return;
  clear(box);
  if (job) box.append(jobLine(job));
}

function jobLine(job) {
  return h("p", {class: "detail"},
    `${code("job", job.kind)} · ${code("phase", job.phase)}${job.total ? ` · ${job.done} / ${job.total}` : ""}`,
    job.item ? ` · ${job.item}` : "", job.message ? ` · ${job.message}` : "");
}

function minuteControls(context, message) {
  const act = (path, body, done) => async () => {
    try {
      await post(path, body);
      if (done) message.textContent = done;
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  const start = button(t("data.backfill"), act("/api/minute/backfill", {}, t("data.backfill_started")), "primary");
  const stop = button(t("data.stop_backfill"), act("/api/jobs/MINUTE/cancel", {}, ""));
  start.id = "backfill-start";
  stop.id = "backfill-stop";
  return h("div", {class: "actions"}, start, stop);
}

function showMinute(root, context) {
  // IB's minute bars (§17.3): whether they are on, the backfill's progress, and each market IB refused (§5.4).
  const box = root.querySelector("#minute-job");
  if (!box) return;
  const ib = context.state.app.services.ib;
  const job = context.state.jobs.MINUTE;
  root.querySelector("#backfill-start").disabled = !ib.configured || Boolean(job);
  root.querySelector("#backfill-stop").disabled = !job;
  append(clear(box), [
    ib.configured ? null : h("p", {class: "quiet", text: t("data.ib_off")}),
    job ? jobLine(job) : null,
    Object.entries(ib.permissions).filter(([, permission]) => permission === "NO_PERMISSION")
      .map(([market]) => h("p", {class: "warning", text: t("data.no_permission", {market: code("market", market)})})),
  ]);
}

async function startUpdate(market, context, message) {
  try {
    await post("/api/update", {market});
    message.textContent = t("data.update_started", {market: code("market", market)});
    context.refresh();
  } catch (error) {
    message.textContent = errorText(error);
  }
}

function showSpylead(root, snapshot, context, message) {
  // spylead's stored series, F and COMPOSITE marked in-sample, and [Snapshot now] (§5.6).
  const start = button(t("data.snapshot"), async () => {
    try {
      await post("/api/spylead/snapshot");
      message.textContent = t("data.snapshot_started");
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  });
  start.disabled = !snapshot.configured;
  let status = t("data.spylead_off");
  if (snapshot.configured) status = snapshot.snapshot_at ? t("data.spylead_at", {time: moment(snapshot.snapshot_at)}) : t("data.spylead_never");
  append(clear(root), [
    h("div", {class: "actions"}, start, h("span", {class: "detail", text: status})),
    snapshot.series.length ? h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, [t("data.series_name"), t("data.series_first"), t("data.series_last"), t("data.series_in_sample")]
        .map((label) => h("th", {text: label})))),
      h("tbody", {}, snapshot.series.map((series) => h("tr", {},
        h("td", {class: "symbol", text: series.name}),
        h("td", {class: "nowrap", text: day(series.first_day)}),
        h("td", {class: "nowrap", text: day(series.last_day)}),
        h("td", {class: series.in_sample ? "warning" : "", text: series.in_sample ? t("data.in_sample") : "—"})))))) : null,
  ]);
}

function createForm(reload, message) {
  const name = h("input", {type: "text", maxlength: "80"});
  const market = select(MARKETS.map((value) => [value, code("market", value)]), "US");
  const form = h("form", {class: "inline", on: {submit: async (event) => {
    event.preventDefault();
    try {
      await post("/api/lists", {name: name.value, market: market.value});
      name.value = "";
      reload();
    } catch (error) {
      showErrors(form, [error]);
      message.textContent = errorText(error);
    }
  }}},
  field(t("data.list_name"), name, "name"), field(t("data.market"), market, "market"),
  h("button", {type: "submit", class: "button", text: t("data.create")}));
  return form;
}

function showLists(root, lists, reload, message) {
  clear(root);
  if (!lists.length) root.append(h("p", {class: "quiet", text: t("data.no_lists")}));
  for (const found of lists) root.append(listCard(found, reload, message));
}

function listCard(found, reload, message) {
  // Rename, delete, and the members: add, remove and reorder; every change saves the whole list.
  const card = h("article", {class: "card"});
  const symbols = [...found.symbols];
  const save = async (body) => {
    try {
      await put(`/api/lists/${found.id}`, body);
      reload();
    } catch (error) {
      showErrors(card, [error]);
      message.textContent = errorText(error);
    }
  };
  const name = h("input", {type: "text", value: found.name});
  const adding = h("input", {type: "text", placeholder: t("data.symbol_placeholder"), autocapitalize: "characters"});
  card.append(
    h("div", {class: "inline"},
      field(t("data.list_name"), name, "name"),
      button(t("data.rename"), () => save({name: name.value})),
      button(t("data.delete"), async () => {
        if (!confirm(t("data.confirm_delete_list", {name: found.name}))) return;
        try {
          await remove(`/api/lists/${found.id}`);
          reload();
        } catch (error) {
          message.textContent = errorText(error);
        }
      }, "danger")),
    h("p", {class: "detail", text: code("market", found.market)}),
    h("ol", {class: "members", data: {path: "symbols"}}, symbols.map((symbol, index) => h("li", {},
      h("span", {class: "symbol", text: symbol}),
      button("↑", () => save({symbols: moved(symbols, index, -1)})),
      button("↓", () => save({symbols: moved(symbols, index, 1)})),
      button("×", () => save({symbols: symbols.filter((_, other) => other !== index)}), "danger")))),
    h("div", {class: "inline"},
      field(t("data.add_symbols"), adding, "symbols"),
      button(t("data.add"), () => save({symbols: [...symbols, ...adding.value.split(/[\s,]+/).filter(Boolean)]}))));
  return card;
}

function moved(symbols, index, shift) {
  const target = index + shift;
  if (target < 0 || target >= symbols.length) return symbols;
  const copy = [...symbols];
  [copy[index], copy[target]] = [copy[target], copy[index]];
  return copy;
}

function showSymbols(root, market, rows, categories, reload, message) {
  clear(root);
  if (!rows.length) return root.append(h("p", {class: "quiet", text: t("data.no_symbols")}));
  const change = async (symbol, body) => {
    try {
      await patch(`/api/symbols/${market}/${encodeURIComponent(symbol)}`, body);
      reload();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  const purge = async (symbol) => {
    if (!confirm(t("data.confirm_purge", {symbol}))) return;
    try {
      await remove(`/api/symbols/${market}/${encodeURIComponent(symbol)}`);
      reload();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  const headings = ["symbol", "name", "category", "exchange", "range", "last_error", "minute", ""];
  root.append(h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, headings.map((key) => h("th", {text: key ? t(`data.column_${key}`) : ""})))),
    h("tbody", {}, rows.map((row) => h("tr", {},
      h("td", {class: "symbol", text: row.symbol}),
      h("td", {text: row.name || "—"}),
      h("td", {}, select(categories.map((value) => [value, code("category", value)]), row.category,
        (value) => change(row.symbol, {category: value})),
      h("span", {class: "detail", text: ` ${code("category_source", row.category_source)}`})),
      h("td", {text: row.exchange || "—"}),
      h("td", {class: "nowrap", text: row.first_day ? `${day(row.first_day)} – ${day(row.last_day)}` : "—"}),
      h("td", {text: fetchError(row.last_error)}),
      h("td", {}, checkbox(row.minute_enabled, (value) => change(row.symbol, {minute_enabled: value})),
        h("span", {class: "detail", text: ` ${coverage(row.coverage)}`})),
      h("td", {}, button(t("data.purge"), () => purge(row.symbol), "danger"))))))));
}

function fetchError(text) {
  // "SYMBOL_UNKNOWN: ..." shows its code in words, then the source's English detail.
  if (!text) return "—";
  const [head, ...rest] = text.split(": ");
  const name = head.startsWith("HTTP_") ? `${code("fetch", "HTTP")} ${head.slice(5)}` : code("fetch", head);
  return `${name}${rest.length ? ` — ${rest.join(": ")}` : ""}`;
}

function coverage(counts) {
  const parts = Object.entries(counts || {}).map(([status, count]) => `${code("coverage", status)} ${number(count, 0)}`);
  return parts.length ? parts.join(" · ") : t("data.no_coverage");
}
