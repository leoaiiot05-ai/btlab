// Compare: 2 to 4 results, their curves rescaled to 1 and overlaid, and their metrics side by side (SPEC.md §20.2).

import {get} from "../lib/api.js";
import {base, box, line, mount, points, ranges} from "../lib/charts.js";
import {RUNS} from "../lib/colors.js";
import {h} from "../lib/dom.js";
import {errorText, t} from "../lib/i18n.js";
import {METRICS, format, toneOf} from "../lib/metrics.js";

export async function render(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  root.append(h("p", {}, h("a", {href: "#results", text: t("results.back")})), h("h2", {text: t("compare.title")}), message);
  let found;
  try {
    found = await get(`/api/compare?ids=${encodeURIComponent(context.arg)}`);
  } catch (error) {
    message.textContent = errorText(error);
    return;
  }
  const container = box();
  const chart = mount(container, () => ({
    ...base(),
    series: found.runs.map((run, index) => line(run.name, points(found.t, found.series[String(run.id)], "day"), RUNS[index % RUNS.length],
      {connectNulls: true})),
  }));
  const last = found.t.length ? found.t[found.t.length - 1] * 86400000 : 0;
  root.append(ranges(chart, "day", last), container,
    h("div", {class: "table-scroll"}, h("table", {class: "table"},
      h("thead", {}, h("tr", {}, h("th", {text: t("compare.metric")}), found.runs.map((run) => h("th", {text: run.name})))),
      h("tbody", {}, METRICS.map(([key, kind]) => h("tr", {},
        h("th", {text: t(`metric.${key}`)}),
        found.runs.map((run) => h("td", {class: `number ${toneOf(kind, run.metrics[key])}`,
          text: format(kind, run.metrics[key])}))))))));
}
