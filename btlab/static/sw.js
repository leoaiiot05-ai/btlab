// The service worker (SPEC.md §20.5): the app shell cached by version and served stale-while-revalidate, API GETs from
// the network with the saved results as their fallback, and every other method left to the network alone.

// A hash of the shell's contents, which tests/test_static.py recomputes: a changed shell file needs a new value, and the
// changed worker then installs a fresh shell cache.
const VERSION = "56b7d968c39c";
const SHELL_CACHE = `btlab-shell-${VERSION}`;
const RESULTS_CACHE = "btlab-results";
// Tailscale Serve answers these while btlab is down: no answer from btlab, like a failed network.
const GATEWAY_ERRORS = [502, 503, 504];
const SHELL = [
  "/",
  "/app.js",
  "/icons/apple-touch-icon.png",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/index.html",
  "/lib/api.js",
  "/lib/charts.js",
  "/lib/colors.js",
  "/lib/dom.js",
  "/lib/i18n.js",
  "/lib/metrics.js",
  "/lib/offline.js",
  "/locales/en.json",
  "/locales/zh-TW.json",
  "/manifest.webmanifest",
  "/style.css",
  "/vendor/echarts/echarts.min.js",
  "/views/backtest.js",
  "/views/compare.js",
  "/views/data.js",
  "/views/home.js",
  "/views/live.js",
  "/views/optimize.js",
  "/views/results.js",
  "/views/rules.js",
  "/views/settings.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key.startsWith("btlab-shell-") && key !== SHELL_CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  // POST, PUT, PATCH and DELETE never use a cache.
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) event.respondWith(networkFirst(request));
  else if (SHELL.includes(url.pathname)) event.respondWith(staleWhileRevalidate(event));
});

async function networkFirst(request) {
  const response = await fetch(request).catch(() => Response.error());
  if (response.type !== "error" && !GATEWAY_ERRORS.includes(response.status)) return response;
  return (await caches.match(request, {cacheName: RESULTS_CACHE})) || response;
}

async function staleWhileRevalidate(event) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(event.request, {ignoreSearch: true});
  const fresh = fetch(event.request).then(async (response) => {
    if (response.ok) await cache.put(event.request, response.clone());
    return response;
  });
  // The cached copy answers at once; the worker stays alive until the fresh one is stored.
  event.waitUntil(fresh.catch(() => {}));
  return cached || fresh;
}
