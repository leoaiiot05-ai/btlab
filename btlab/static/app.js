// The shell: login, navigation, the run-mode badge, polling, the language toggle and logout (SPEC.md §20.2, §20.4),
// and offline mode with the saved results (§20.5).

import {UNAUTHORIZED, get, post, setOffline} from "./lib/api.js";
import {button, clear, h, select} from "./lib/dom.js";
import {code, current, errorText, load, moment, preferred, t} from "./lib/i18n.js";
import * as offline from "./lib/offline.js";
import * as backtest from "./views/backtest.js";
import * as compare from "./views/compare.js";
import * as data from "./views/data.js";
import * as home from "./views/home.js";
import * as later from "./views/later.js";
import * as optimize from "./views/optimize.js";
import * as results from "./views/results.js";
import * as rules from "./views/rules.js";
import * as settings from "./views/settings.js";

const TABS = ["home", "data", "backtest", "results", "optimize", "live", "settings"];
const VIEWS = {home, data, backtest, rules, results, compare, optimize, settings, live: later};
// Views opened from a tab's view keep that tab lit.
const PARENTS = {rules: "backtest", compare: "results"};
// What still works offline: the stored results, and the settings.
const OFFLINE_VIEWS = ["results", "settings"];
const FAST_MS = 2000;
const SLOW_MS = 15000;

const context = {
  state: null,
  offline: false,
  // A rule drafted in the rule builder, handed to the Backtest view (§20.2 item 4).
  draftRule: null,
  // The Backtest view's current form, which the Optimize view can start from (§20.2 item 6).
  draftConfig: null,
  // The open view and its argument: #results/12 is the view "results" with "12".
  name: "home",
  arg: "",
  refresh: () => poll(),
  go: (view) => {
    location.hash = view;
  },
  logout: () => logout(),
  language: (name) => switchLanguage(name),
  // Keeps the results cache equal to the server's offline set; refresh re-stores one result just seen online.
  sync: (refresh = null) => (context.offline ? Promise.resolve() : offline.sync(refresh).catch(() => {})),
};
let timer = null;
let lastEvent = 0;

async function boot() {
  await load(preferred());
  window.addEventListener(UNAUTHORIZED, showLogin);
  window.addEventListener("hashchange", () => render());
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    seen(await get("/api/state"));
  } catch (error) {
    if (error.status === 401) return showLogin();
    // No answer at all: the stored results only (§20.5).
    setMode(true);
    return showShell();
  }
  showShell();
  context.sync();
}

function seen(state) {
  context.state = state;
  offline.remember(state.app.server_time);
}

function setMode(isOffline) {
  context.offline = isOffline;
  setOffline(isOffline);
  document.body.classList.toggle("offline", isOffline);
  const banner = document.getElementById("offline");
  if (!banner) return;
  banner.hidden = !isOffline;
  banner.textContent = isOffline ? t("shell.offline", {time: moment(offline.lastSeen())}) : "";
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
      seen(await get("/api/state"));
      showShell();
      context.sync();
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
    h("div", {class: "banners"},
      h("div", {id: "banner", class: "banner live", hidden: true}),
      h("div", {id: "offline", class: "banner offline", role: "status", hidden: true})),
    h("nav", {class: "tabs", "aria-label": t("shell.views")},
      TABS.map((name) => h("a", {href: `#${name}`, id: `tab-${name}`, class: "tab", text: t(`view.${name}`)}))),
    h("main", {id: "view"}));
  setMode(context.offline);
  showBadge();
  render();
  schedule();
}

function route() {
  const [name, ...rest] = location.hash.slice(1).split("/");
  return [name in VIEWS ? name : "home", rest.join("/")];
}

function render() {
  // The open view, from the hash. Views share the context, so what one hands over (a rule, a form) reaches another.
  const [name, arg] = route();
  Object.assign(context, {name, arg});
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-current", tab.id === `tab-${PARENTS[name] || name}` ? "page" : "false");
  }
  const main = document.getElementById("view");
  if (!main) return;
  clear(main);
  if (context.offline && !OFFLINE_VIEWS.includes(name)) {
    main.append(h("h2", {text: t(`view.${PARENTS[name] || name}`)}), h("p", {class: "quiet", text: t("shell.needs_connection")}),
      h("a", {href: "#results", text: t("shell.saved_results")}));
    return;
  }
  VIEWS[name].render(main, context);
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
    seen(await get("/api/state"));
    showBadge();
    if (context.offline) {
      // Back online by itself: everything works again, and the cache catches up.
      setMode(false);
      render();
      context.sync();
    }
    const newest = context.state.events.length ? context.state.events[0].id : 0;
    const view = VIEWS[route()[0]];
    const main = document.getElementById("view");
    if (newest > lastEvent && lastEvent && view.refreshOn && view.refreshOn(context.state.events, lastEvent)) render();
    else if (view.update && main) view.update(main, context);
    lastEvent = newest;
  } catch (error) {
    if (error.status === 401) return;
    if (!context.offline) {
      setMode(true);
      render();
    }
  }
  schedule();
}

function schedule() {
  // Every 2 s while a job runs, otherwise every 15 s (§20.4); offline, every 15 s until btlab answers.
  const busy = !context.offline && context.state && Object.values(context.state.jobs).some(Boolean);
  timer = setTimeout(poll, busy ? FAST_MS : SLOW_MS);
}

async function switchLanguage(name) {
  await load(name);
  showShell();
}

async function logout() {
  // Logging out deletes the saved results (§20.5).
  try {
    await post("/api/logout");
  } catch {
    // Offline the server cannot be told; the saved results go anyway.
  } finally {
    await offline.clear();
    setMode(false);
    showLogin();
  }
}

boot();
