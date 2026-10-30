// What every chart shares (SPEC.md §20.3): the vendored ECharts loaded on the first chart, the canvas renderer,
// richText tooltips, a time axis, pinch, drag and slider zoom, range buttons, and the light or dark theme.

import {button, h} from "./dom.js";
import {t} from "./i18n.js";

const DAY_MS = 86400000;
const DARK = window.matchMedia("(prefers-color-scheme: dark)");
const RANGES = {
  day: [["1M", 31], ["6M", 183], ["1Y", 366], ["5Y", 1827], ["ALL", 0]],
  second: [["1D", 1], ["5D", 5], ["1M", 31], ["ALL", 0]],
};

let loading = null;

export function loadECharts() {
  // A classic script from vendor/, loaded once; the login page never needs it.
  if (window.echarts) return Promise.resolve(window.echarts);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = h("script", {src: "/vendor/echarts/echarts.min.js"});
      script.addEventListener("load", () => resolve(window.echarts));
      script.addEventListener("error", reject);
      document.head.append(script);
    });
  }
  return loading;
}

export function millis(times, unit) {
  // Day numbers or epoch seconds, as the milliseconds of a time axis.
  const scale = unit === "day" ? DAY_MS : 1000;
  return times.map((value) => value * scale);
}

export function points(times, values, unit) {
  const moments = millis(times, unit);
  return moments.map((moment, index) => [moment, values[index]]);
}

export function base() {
  return {
    backgroundColor: "transparent",
    animation: false,
    tooltip: {trigger: "axis", renderMode: "richText", confine: true},
    legend: {top: 0, type: "scroll"},
    grid: {left: 60, right: 16, top: 36, bottom: 72},
    xAxis: {type: "time"},
    yAxis: {type: "value", scale: true},
    dataZoom: [{type: "inside"}, {type: "slider", height: 24, bottom: 12}],
  };
}

export function line(name, data, color, extra = {}) {
  return {name, type: "line", data, showSymbol: false, sampling: "lttb", lineStyle: {width: 1.5}, itemStyle: {color}, ...extra};
}

export function mount(container, build) {
  // A chart whose option build() makes; it redraws with the colour scheme and resizes with the window, and it
  // lets go of both once its container leaves the page.
  const holder = {instance: null};
  const draw = (echarts) => {
    if (holder.instance) holder.instance.dispose();
    holder.instance = echarts.init(container, DARK.matches ? "dark" : null, {renderer: "canvas"});
    holder.instance.setOption(build());
  };
  loadECharts().then((echarts) => {
    draw(echarts);
    const scheme = () => (container.isConnected ? draw(echarts) : stop());
    const resize = () => (container.isConnected ? holder.instance.resize() : stop());
    const stop = () => {
      DARK.removeEventListener("change", scheme);
      window.removeEventListener("resize", resize);
      if (holder.instance) holder.instance.dispose();
    };
    DARK.addEventListener("change", scheme);
    window.addEventListener("resize", resize);
  });
  return {
    dispatch: (action) => holder.instance && holder.instance.dispatchAction(action),
    update: (option) => holder.instance && holder.instance.setOption(option),
  };
}

export function ranges(chart, unit, last) {
  // 1M, 6M, 1Y, 5Y and All on daily runs; 1D, 5D, 1M and All on minute ones (§20.3).
  return h("div", {class: "actions ranges"}, RANGES[unit].map(([name, days]) => button(t(`chart.range_${name}`), () => {
    if (!days) chart.dispatch({type: "dataZoom", start: 0, end: 100});
    else chart.dispatch({type: "dataZoom", startValue: last - days * DAY_MS, endValue: last});
  })));
}

export function box(className = "chart") {
  return h("div", {class: className});
}
