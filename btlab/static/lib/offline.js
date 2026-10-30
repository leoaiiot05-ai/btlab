// Saved results for offline reading (SPEC.md §20.5). The server chooses them (GET /api/offline); the app keeps the
// results cache equal to that set: per result its detail, its series and its first 500 trades.

export const RESULTS_CACHE = "btlab-results";
// The trades table's page: the cache holds the first one, so a stored result shows its trades offline.
export const TRADES_PAGE = 500;
const LAST_STATE_KEY = "btlab.last_state";
// Caching a result is not opening it: the server leaves its opened_at alone (§20.5).
const SYNC = {"X-Btlab-Sync": "1"};

export function urls(id) {
  return [`/api/backtests/${id}`, `/api/backtests/${id}/series`, `/api/backtests/${id}/trades?offset=0&limit=${TRADES_PAGE}`];
}

export async function sync(refresh = null) {
  // Stores the missing results and deletes the others; refresh re-stores one result, as it was just seen online.
  if (!("caches" in window)) return;
  const response = await fetch("/api/offline", {credentials: "same-origin"});
  if (!response.ok) return;
  const {ids} = await response.json();
  const cache = await caches.open(RESULTS_CACHE);
  const wanted = new Set(ids.flatMap(urls));
  for (const request of await cache.keys()) {
    const url = new URL(request.url);
    const key = url.pathname + url.search;
    if (!wanted.has(key) || (refresh !== null && urls(refresh).includes(key))) await cache.delete(request);
  }
  for (const url of wanted) {
    if (await cache.match(url)) continue;
    const answer = await fetch(url, {credentials: "same-origin", headers: SYNC});
    if (answer.ok) await cache.put(url, answer);
  }
}

export async function stored() {
  // The details of the stored results, newest first, for the offline list.
  if (!("caches" in window)) return [];
  const cache = await caches.open(RESULTS_CACHE);
  const details = [];
  for (const request of await cache.keys()) {
    if (/^\/api\/backtests\/\d+$/.test(new URL(request.url).pathname)) details.push(await (await cache.match(request)).json());
  }
  return details.sort((a, b) => b.id - a.id);
}

export async function clear() {
  if ("caches" in window) await caches.delete(RESULTS_CACHE);
}

export function remember(serverTime) {
  // The time of the last successful state, which the offline banner shows.
  try {
    localStorage.setItem(LAST_STATE_KEY, serverTime);
  } catch {
    // Without storage the banner says "—".
  }
}

export function lastSeen() {
  try {
    return localStorage.getItem(LAST_STATE_KEY);
  } catch {
    return null;
  }
}
