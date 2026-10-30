# Prompt 19: PWA and offline viewing

Covers SPEC.md §19 (`GET /api/offline`), §20.5 and §21. Acceptance: AC-22.

## 1. Steps

1. **`btlab/static/sw.js`**, scope "/":
   - the shell (`/`, `index.html`, `app.js` and its modules, `style.css`, the locales, the vendored ECharts, the icons, the manifest) pre-cached on install under `btlab-shell-<version>`; activation deletes older shell caches; shell requests are served stale-while-revalidate;
   - API GETs go to the network, and fall back to `btlab-results` when the network fails;
   - every other method passes through untouched and never meets a cache.
   - `<version>` is a hash of the shell files' contents, so any change to the shell installs a fresh cache; a test recomputes it.
2. **`lib/offline.js`** and the shell:
   - `app.js` registers the worker;
   - the results cache follows `GET /api/offline`: per result its detail, its series and its first 500 trades, stored when missing and deleted when no longer listed; synced at startup, after opening a result (refreshing that result) and after a keep-offline change or a rename. The sync's detail requests carry `X-Btlab-Sync: 1`, and the server then leaves `opened_at` alone, so caching a kept result does not count as opening it;
   - **offline mode** when `GET /api/state` fails without an answer (at startup too): the banner "Offline — saved results only (as of …)" with the time of the last successful state (kept in `localStorage` inside try/catch); Results lists the stored results only, a stored result opens from the cache without its actions, other views say they need the connection, and `lib/api.js` refuses every write; polling goes on, and the first answer returns the app to online by itself;
   - logout deletes the results cache.
3. **`web.py`**: `sw.js` gets `text/javascript` and the §21 headers like every static file (with `no-cache`, so a new worker is found at once); `GET /api/backtests/{id}` with `X-Btlab-Sync: 1` does not touch `opened_at`.
4. **Tests** (AC-22, in `tests/test_static.py` and `tests/test_web_results.py`):
   - the shell list in `sw.js` equals the files it must cache, computed from the folder, and its version is the hash of their contents;
   - the worker answers only GET requests from its caches;
   - logout deletes the results cache (a static check of that code path);
   - the offline strings exist in both locales;
   - `sw.js` is served with its content type and the §21 headers; a sync request leaves `opened_at` as it was.
5. Run `.venv/bin/python -m pytest -q`, smoke-test offline mode in headless Chromium (the worker, the cache, the banner and the recovery), and extend the manual checklist.

### Readings where the spec is silent

- **Opening a result refreshes its cached copy**, so a result read offline is the one last seen online.
- **The trades table pages by 500**, the same first page the cache stores, so a cached result shows its trades offline.
- **Offline, the candles of a result** are not stored (§20.5 stores detail, series and trades); their chart says it needs the connection.

## 2. Extension points

- **A shell file** joins the list in `sw.js` (the test names any file missing).
- **A cached request per result** is one URL in `lib/offline.js`.

## 3. Refactor seams

- What the cache holds is decided by the server (`GET /api/offline`); the app only follows it.

## 4. Deferred items (appended to TODO.md)

- None.

## Manual checklist (on the PC, the iPhone and the iPad)

Over Tailscale Serve, with btlab running on the PC:

1. On the iPhone, open btlab from its home-screen icon (not a Safari tab: a tab's storage can be evicted after 7 days) and log in.
2. Open 3 results, one after the other. Open a 4th result and tick "Keep offline"; or tick it on one of the 3.
3. Settings: "Results saved on this device" counts the results stored (at least 4).
4. Disconnect Tailscale (or stop btlab on the PC) and close the app completely (swipe it away).
5. Reopen it from the home-screen icon:
   - the yellow banner reads "Offline — saved results only (as of <the time btlab last answered>)";
   - Home, Data, Backtest, Optimize and Live say they need the connection, with a link to the saved results;
   - Results lists the stored results only, without compare checkboxes; each of the 4 opens with its metrics, charts and its first 500 trades;
   - a result has no rename, keep-offline, export, compare or delete; its candles say they are not saved offline.
6. Reconnect Tailscale (or start btlab): within 15 s the banner goes away by itself, the open view works again, and a result shows its actions.
7. Log out: Settings then shows 0 results saved on this device after the next login (the cache was deleted).
8. Repeat 1–7 on the iPad; on the PC, repeat 2–6 in Edge (installed app), stopping btlab instead of Tailscale.
9. After an update of btlab's UI, reopen the app twice: the second start runs the new version (the first one is served from the cache while the new shell installs).

## Result

- **`btlab/static/sw.js`** (scope "/"): the shell (27 entries: `/`, the page, `app.js` and its modules, `style.css`, both locales, the manifest, the 3 icons and the vendored ECharts) is pre-cached on install under `btlab-shell-<version>`, where the version is a content hash of the shell (`tests/test_static.py` recomputes it and names the value to set when a shell file changes); activation deletes every other `btlab-shell-*` cache and claims the open pages; shell requests are stale-while-revalidate, with the worker kept alive until the fresh copy is stored. API GETs go to the network and fall back to `btlab-results` when the network fails **or Tailscale Serve answers 502/503/504** (what it answers while btlab is down on a PC that is on); other methods return before the worker answers, so they never meet a cache.
- **`lib/offline.js`**: the results cache follows `GET /api/offline` (detail, series and the first 500 trades per result; missing ones stored, unlisted ones deleted), synced at startup, at login, on recovery, after opening a result (which refreshes that result), after rename, keep offline and delete. The sync reads the detail with `X-Btlab-Sync: 1`, and `web.py` then leaves `opened_at` alone, so storing a kept result is not opening it. The time of the last successful state is kept in `localStorage` (inside try/catch) for the banner.
- **Offline mode** (`app.js`, `lib/api.js`, the views): entered when `GET /api/state` gets no usable answer, at startup or while polling; the banner; Results and Settings work from the cache, other views say they need the connection; result pages hide their actions and their candles; `lib/api.js` refuses every write and reports a failed request as `OFFLINE`; polling goes on every 15 s and the first answer returns the app to online by itself. Logout deletes the results cache whether or not the server answers. The run-mode badge stays hidden until a state arrives, the two banners share one grid row, and disabled buttons now look disabled.
- **Readings**: the trades table pages by 500 (the cached page). Settings shows how many results the device stores, and "Clear saved results" uses the same `offline.clear()` as logout.
- **Tests** (AC-22): the shell list equals the static files computed from the folder and the version is their content hash; the fetch handler returns for any method but GET before it can answer; logout's `finally` clears the cache that `sw.js` falls back on (`btlab-results` in both files); the offline strings exist in both locales; `sw.js` is served as `text/javascript` with the §21 headers and `no-cache`; a sync read leaves `opened_at` and the offline set unchanged. Hashes of committed files ignore CRLF, so a Windows checkout with `core.autocrlf` passes too (this also hardens the ECharts SHA-256 check of prompt 17).
- **Smoke run** in headless Chromium over a scratch server on temporary stores (not `python -m btlab`): worker installed with one shell cache of 27 entries; 3 opened results plus 1 kept by PATCH gave 12 cached requests and the offline set `[3, 2, 1, 4]` (the kept one unopened); offline, the reloaded app showed the banner with the time, the 4 stored results, each opening with charts and trades and no actions; Settings counted 4; a write was refused with `OFFLINE`; back online the app recovered by itself within one poll with its actions; logout deleted `btlab-results`; an older `btlab-shell-*` cache disappeared on activation while `btlab-results` stayed. No page errors; the only console messages were the 401 before login and the failed `/api/state` while offline.
- `.venv/bin/python -m pytest -q`: 1155 passed (also with `-X dev -W error`).
