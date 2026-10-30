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
