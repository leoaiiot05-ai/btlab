// Home: the markets, the jobs per lane with cancel, the services and the latest events (SPEC.md §20.2 item 1).

import {post} from "../lib/api.js";
import {button, h} from "../lib/dom.js";
import {code, day, errorText, moment, number, t} from "../lib/i18n.js";

const CANCELLABLE = ["DATA", "MINUTE", "COMPUTE"];
const EVENTS_SHOWN = 20;

export function render(root, context) {
  const state = context.state;
  root.append(
    h("h2", {text: t("view.home")}),
    h("section", {class: "cards"}, Object.entries(state.markets).map(([market, info]) => marketCard(market, info, state))),
    h("h3", {text: t("home.jobs")}),
    h("section", {id: "jobs", class: "cards"}, jobCards(context)),
    h("h3", {text: t("home.events")}),
    eventList(state.events.slice(0, EVENTS_SHOWN)));
}

export function update(root, context) {
  // Every poll: the job cards only, so the rest of the page stays still.
  const jobs = root.querySelector("#jobs");
  if (jobs) jobs.replaceChildren(...jobCards(context));
}

export function refreshOn() {
  return true;
}

function marketCard(market, info, state) {
  const next = state.app.schedule.next ? state.app.schedule.next[market] : null;
  const last = info.last_update;
  return h("article", {class: "card"},
    h("h4", {text: code("market", market)}),
    row(t("home.last_bar"), day(info.last_day)),
    row(t("home.symbols"), number(info.symbols, 0)),
    row(t("home.last_update"), last ? `${code("outcome", last.outcome)} · ${moment(last.finished_at)}` : "—"),
    last && last.message ? h("p", {class: "detail", text: last.message}) : null,
    row(t("home.next_update"), next ? moment(next) : t("home.schedule_off")));
}

function jobCards(context) {
  return Object.entries(context.state.jobs).map(([lane, job]) => {
    if (!job) return h("article", {class: "card quiet"}, h("h4", {text: code("lane", lane)}), h("p", {text: t("home.idle")}));
    const total = job.total || 0;
    return h("article", {class: "card"},
      h("h4", {text: `${code("lane", lane)} · ${code("job", job.kind)}`}),
      row(t("home.phase"), code("phase", job.phase)),
      total ? h("progress", {max: String(total), value: String(job.done)}) : null,
      total ? row(t("home.progress"), `${number(job.done, 0)} / ${number(total, 0)}`) : null,
      job.item ? row(t("home.item"), job.item) : null,
      job.message ? h("p", {class: "detail", text: job.message}) : null,
      row(t("home.started"), moment(job.started_at)),
      CANCELLABLE.includes(lane) ? cancelButton(lane, context) : null);
  });
}

function cancelButton(lane, context) {
  const message = h("span", {class: "message", role: "alert"});
  return h("div", {class: "actions"}, button(t("home.cancel"), async () => {
    try {
      await post(`/api/jobs/${lane}/cancel`);
      context.refresh();
    } catch (error) {
      message.textContent = errorText(error);
    }
  }), message);
}

function eventList(events) {
  if (!events.length) return h("p", {class: "quiet", text: t("home.no_events")});
  return h("div", {class: "table-scroll"}, h("table", {class: "table"},
    h("thead", {}, h("tr", {}, [t("home.time"), t("home.level"), t("home.kind"), t("home.where"), t("home.message")]
      .map((label) => h("th", {text: label})))),
    h("tbody", {}, events.map((event) => h("tr", {class: `level-${event.level.toLowerCase()}`},
      h("td", {class: "nowrap", text: moment(event.ts, event.market)}),
      h("td", {text: code("level", event.level)}),
      h("td", {text: code("event", event.kind)}),
      h("td", {text: [event.market, event.symbol].filter(Boolean).join(" ")}),
      h("td", {text: event.message}))))));
}

function row(label, value) {
  return h("div", {class: "row"}, h("span", {class: "row-label", text: label}), h("span", {class: "row-value", text: value}));
}
