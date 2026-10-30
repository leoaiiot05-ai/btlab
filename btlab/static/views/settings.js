// Settings: language, logout, credits, the schedule and limits (read-only), and the saved offline results
// (SPEC.md §20.2 item 8).

import {button, h, select} from "../lib/dom.js";
import {current, moment, number, t} from "../lib/i18n.js";
import * as offline from "../lib/offline.js";

export function render(root, context) {
  // Offline from the start there is no state yet: the schedule and the limits wait for btlab.
  const app = context.state ? context.state.app : null;
  root.append(
    h("h2", {text: t("view.settings")}),
    h("section", {class: "card"},
      h("h3", {text: t("settings.language")}),
      select([["zh-TW", "中文"], ["en", "English"]], current(), (value) => context.language(value)),
      button(t("shell.logout"), () => context.logout())),
    h("section", {class: "card"}, h("h3", {text: t("settings.schedule")}), app ? schedule(app) : waiting()),
    h("section", {class: "card"}, h("h3", {text: t("settings.limits")}), app ? limits(app) : waiting()),
    saved(),
    h("section", {class: "card"},
      h("h3", {text: t("settings.credits")}),
      h("p", {text: t("settings.credit_data")}),
      h("p", {text: t("settings.credit_charts")}),
      h("a", {href: "/vendor/echarts/LICENSE", text: t("settings.licence")})));
}

function waiting() {
  return h("p", {class: "quiet", text: t("shell.needs_connection")});
}

function schedule(app) {
  return [
    h("p", {text: app.schedule.enabled ? t("settings.schedule_on") : t("settings.schedule_off")}),
    ...Object.entries(app.schedule.next || {}).map(([market, when]) => h("p", {text: t("settings.next_update", {market, when: moment(when)})})),
  ];
}

function limits(app) {
  return app.limits ? Object.entries(app.limits).map(([key, value]) => h("p", {text: `${t(`settings.limit_${key}`)}: ${number(value)}`}))
    : h("p", {text: t("settings.no_limits")});
}

function saved() {
  // How many results this device keeps for offline reading, and a button to delete them.
  const count = h("p", {role: "status"});
  const show = async () => {
    count.textContent = t("settings.offline_count", {count: (await offline.stored()).length});
  };
  show();
  return h("section", {class: "card"},
    h("h3", {text: t("settings.offline")}),
    h("p", {text: t("settings.offline_detail")}),
    count,
    button(t("settings.clear_offline"), async () => {
      await offline.clear();
      show();
    }));
}
