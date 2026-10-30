// The shell: login, navigation, the run-mode badge, polling, the language toggle and logout (SPEC.md §20.2, §20.4).

import {UNAUTHORIZED, get, post} from "./lib/api.js";
import {button, clear, h, select} from "./lib/dom.js";
import {code, current, errorText, load, preferred, t} from "./lib/i18n.js";
import * as backtest from "./views/backtest.js";
import * as data from "./views/data.js";
import * as home from "./views/home.js";
import * as later from "./views/later.js";
import * as rules from "./views/rules.js";
import * as settings from "./views/settings.js";

const TABS = ["home", "data", "backtest", "results", "optimize", "live", "settings"];
const VIEWS = {home, data, backtest, rules, settings, results: later, optimize: later, live: later};
const FAST_MS = 2000;
const SLOW_MS = 15000;

const context = {
  state: null,
  // A rule drafted in the rule builder, handed to the Backtest view (§20.2 item 4).
  draftRule: null,
  refresh: () => poll(),
  go: (view) => {
    location.hash = view;
  },
  logout: () => logout(),
  language: (name) => switchLanguage(name),
};
let timer = null;
let lastEvent = 0;

async function boot() {
  await load(preferred());
  window.addEventListener(UNAUTHORIZED, showLogin);
  window.addEventListener("hashchange", () => render());
  try {
    context.state = await get("/api/state");
  } catch {
    return showLogin();
  }
  showShell();
}

function showLogin() {
  clearTimeout(timer);
  const root = clear(document.getElementById("app"));
  const token = h("input", {type: "password", autocomplete: "current-password", id: "token"});
  const message = h("p", {class: "message", role: "alert"});
  const submit = async (event) => {
    event.preventDefault();
    try {
      await post("/api/login", {token: token.value});
      context.state = await get("/api/state");
      showShell();
    } catch (error) {
      message.textContent = errorText(error);
    }
  };
  root.append(h("form", {class: "login", on: {submit}},
    h("h1", {text: "btlab"}),
    h("label", {class: "field", for: "token"}, h("span", {class: "field-label", text: t("login.token")}), token),
    h("button", {type: "submit", class: "button primary", text: t("login.submit")}),
    message));
  token.focus();
}

function showShell() {
  const root = clear(document.getElementById("app"));
  root.append(
    h("header", {class: "top"},
      h("span", {class: "brand", text: "btlab"}),
      h("span", {id: "badge", class: "badge"}),
      h("span", {class: "spacer"}),
      select([["zh-TW", "中文"], ["en", "English"]], current(), switchLanguage),
      button(t("shell.logout"), logout)),
    h("div", {id: "banner", class: "banner live", hidden: true}),
    h("nav", {class: "tabs", "aria-label": t("shell.views")},
      TABS.map((name) => h("a", {href: `#${name}`, id: `tab-${name}`, class: "tab", text: t(`view.${name}`)}))),
    h("main", {id: "view"}));
  showBadge();
  render();
  schedule();
}

function render() {
  // The open view, from the hash; the rule builder opens from the Backtest view.
  const name = location.hash.slice(1) || "home";
  const view = VIEWS[name] || home;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-current", tab.id === `tab-${name === "rules" ? "backtest" : name}` ? "page" : "false");
  }
  const main = document.getElementById("view");
  if (!main) return;
  clear(main);
  view.render(main, {...context, name});
}

function showBadge() {
  // TEST, TRADE·paper, or TRADE·live with a red banner: impossible to miss (§20.2 item 1).
  const badge = document.getElementById("badge");
  const banner = document.getElementById("banner");
  if (!badge || !context.state) return;
  const app = context.state.app;
  const kind = app.services && app.services.ib ? app.services.ib.account_kind : null;
  const live = app.mode === "trade" && kind === "live";
  badge.className = `badge mode-${app.mode}${live ? " mode-live" : ""}`;
  badge.textContent = app.mode === "trade" ? `${code("mode", "trade")}${kind ? ` · ${code("account", kind)}` : ""}` : code("mode", "test");
  banner.hidden = !live;
  banner.textContent = live ? t("shell.live_banner") : "";
}

async function poll() {
  clearTimeout(timer);
  try {
    context.state = await get("/api/state");
    showBadge();
    const newest = context.state.events.length ? context.state.events[0].id : 0;
    const name = location.hash.slice(1) || "home";
    const view = VIEWS[name];
    const main = document.getElementById("view");
    if (newest > lastEvent && lastEvent && view && view.refreshOn && view.refreshOn(context.state.events, lastEvent)) render();
    else if (view && view.update && main) view.update(main, {...context, name});
    lastEvent = newest;
  } catch (error) {
    if (error.status === 401) return;
    const banner = document.getElementById("banner");
    if (banner) {
      banner.hidden = false;
      banner.textContent = t("shell.unreachable");
    }
  }
  schedule();
}

function schedule() {
  // Every 2 s while a job runs, otherwise every 15 s (§20.4).
  const busy = context.state && Object.values(context.state.jobs).some(Boolean);
  timer = setTimeout(poll, busy ? FAST_MS : SLOW_MS);
}

async function switchLanguage(name) {
  await load(name);
  showShell();
}

async function logout() {
  try {
    await post("/api/logout");
  } finally {
    if ("caches" in window) await caches.delete("btlab-results");
    showLogin();
  }
}

boot();
