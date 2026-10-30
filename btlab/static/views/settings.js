// Settings: language, logout, credits, the schedule and limits (read-only), and the saved offline results
// (SPEC.md §20.2 item 8).

import {button, h, select} from "../lib/dom.js";
import {current, moment, number, t} from "../lib/i18n.js";

const RESULTS_CACHE = "btlab-results";

export function render(root, context) {
  const app = context.state.app;
  const message = h("p", {class: "message", role: "status"});
  root.append(
    h("h2", {text: t("view.settings")}),
    h("section", {class: "card"},
      h("h3", {text: t("settings.language")}),
      select([["zh-TW", "中文"], ["en", "English"]], current(), (value) => context.language(value)),
      button(t("shell.logout"), () => context.logout())),
    h("section", {class: "card"},
      h("h3", {text: t("settings.schedule")}),
      h("p", {text: app.schedule.enabled ? t("settings.schedule_on") : t("settings.schedule_off")}),
      app.schedule.next ? Object.entries(app.schedule.next).map(([market, when]) =>
        h("p", {text: t("settings.next_update", {market, when: moment(when)})})) : null),
    h("section", {class: "card"},
      h("h3", {text: t("settings.limits")}),
      app.limits ? Object.entries(app.limits).map(([key, value]) => h("p", {text: `${t(`settings.limit_${key}`)}: ${number(value)}`}))
        : h("p", {text: t("settings.no_limits")})),
    h("section", {class: "card"},
      h("h3", {text: t("settings.offline")}),
      h("p", {text: t("settings.offline_detail")}),
      button(t("settings.clear_offline"), async () => {
        if ("caches" in window) await caches.delete(RESULTS_CACHE);
        message.textContent = t("settings.cleared");
      }),
      message),
    h("section", {class: "card"},
      h("h3", {text: t("settings.credits")}),
      h("p", {text: t("settings.credit_data")}),
      h("p", {text: t("settings.credit_charts")}),
      h("a", {href: "/vendor/echarts/LICENSE", text: t("settings.licence")})));
}
