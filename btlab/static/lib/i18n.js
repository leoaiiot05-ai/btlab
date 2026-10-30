// Every string lives in locales/<language>.json, flat key to string (SPEC.md §20.4).

const LANGUAGES = ["zh-TW", "en"];
const STORAGE_KEY = "btlab.language";
const ZONES = {US: "America/New_York", TW: "Asia/Taipei"};
const DAY_MS = 86400000;

let strings = {};
let language = "en";

export function preferred() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (LANGUAGES.includes(stored)) return stored;
  } catch {
    // Private windows may refuse storage; the browser's language decides then.
  }
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh-TW" : "en";
}

export async function load(name) {
  const response = await fetch(`/locales/${name}.json`, {credentials: "same-origin"});
  strings = await response.json();
  language = name;
  document.documentElement.lang = name;
  try {
    localStorage.setItem(STORAGE_KEY, name);
  } catch {
    // Not kept: the next start falls back to the browser's language.
  }
}

export function current() {
  return language;
}

export function t(key, values = {}) {
  const text = strings[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (name in values ? String(values[name]) : whole));
}

export function code(prefix, value) {
  // A backend code in words; an unknown one shows as itself.
  if (value === null || value === undefined) return "—";
  const key = `${prefix}.${value}`;
  return key in strings ? strings[key] : String(value);
}

export function number(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat(language, {maximumFractionDigits: digits, minimumFractionDigits: 0}).format(value);
}

export function percent(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(language, {style: "percent", maximumFractionDigits: digits, minimumFractionDigits: digits})
    .format(value);
}

export function day(value) {
  // A day number (days since 1970-01-01) or an ISO date, as a date.
  if (value === null || value === undefined) return "—";
  const date = typeof value === "number" ? new Date(value * DAY_MS) : new Date(`${value}T00:00:00Z`);
  return new Intl.DateTimeFormat(language, {dateStyle: "medium", timeZone: "UTC"}).format(date);
}

export function moment(iso, market) {
  // A UTC time in the market's time zone, or in the device's when no market applies (§20.4).
  if (!iso) return "—";
  const options = {dateStyle: "short", timeStyle: "short"};
  if (market in ZONES) options.timeZone = ZONES[market];
  return new Intl.DateTimeFormat(language, options).format(new Date(iso));
}

export function errorText(error) {
  // An API error in words: its code localized, then the server's English detail.
  if (!error || !error.code) return t("error.UNKNOWN");
  return `${code("error", error.code)}${error.message ? ` — ${error.message}` : ""}`;
}
