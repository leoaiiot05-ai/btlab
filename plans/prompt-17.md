# Prompt 17: Web UI: shell, data, backtest form and rule builder

Covers SPEC.md §19 (state document, payloads, errors), §20 (items 1 to 4 and 8 of §20.2, §20.3 to §20.5 where they touch the shell) and §21. Acceptance: the checks of AC-21 that exist so far.

## 1. Steps

1. **`btlab/static/`**, no framework and no build step, ES modules loaded by `index.html`:
   - `index.html`: the page skeleton, the manifest, the apple-touch icon, the `apple-mobile-web-app-capable` and `mobile-web-app-capable` metas, `viewport-fit=cover`; no inline script or style.
   - `app.js`: the shell. It loads the locale, asks `GET /api/state`, shows the login form on 401, then the views in a bottom tab bar on phones and a side bar from 900 px; the run-mode badge (TEST, TRADE·paper, TRADE·live, a live account adding a red banner); polling every 2 s while a job runs and every 15 s otherwise, refreshing the open view when an event concerns it; the language toggle (kept in `localStorage` inside try/catch) and logout.
   - `lib/api.js` (fetch with JSON, the error format of §19, 401 back to the login), `lib/i18n.js` (flat locale keys, `Intl.NumberFormat`, times in the market's time zone), `lib/dom.js` (elements built with `textContent` and DOM APIs only; number fields with − and + steppers of 44 px).
   - Views: `views/home.js` (item 1: mode, markets with last bar, last update and next update, jobs per lane with cancel, the latest events), `views/data.js` (item 2: lists with create, rename, delete, add, remove and reorder; the symbols table with category override, minute toggle and coverage, purge; Update US and TW with progress), `views/backtest.js` (item 3: the run form of §10.1 in groups, the parameter form generated from the strategy's schema, plugin load errors, costs prefilled per market, errors shown at their fields by JSON path), `views/rules.js` (item 4: the rule builder), `views/settings.js` (item 8: language, logout, credits for Tiingo, FinMind and FRED, the ECharts licence, schedule and limits read-only). Results, Optimize and Live say they arrive with their prompts.
   - `style.css`: light and dark by `prefers-color-scheme`, dense tables scrolling in their own containers, tabular numbers, touch targets of at least 44 px, safe-area insets, nothing that needs hover; statuses carry text or signs, never colour alone.
   - `locales/zh-TW.json` and `locales/en.json`: flat keys; every code the backend emits so far, under a prefix per kind (`event.`, `flag.`, `error.`, `rule.`, `fetch.`, `outcome.`, `phase.`, `job.`, `lane.`, `exit.`, `coverage.`, …).
   - `manifest.webmanifest` (name and short name btlab, `start_url` and `scope` "/", `display` standalone, theme and background colours, PNG icons of 192 and 512 px, the 512 one maskable) and `icons/` with the 180 px apple-touch icon.
2. **`scripts/make_icons.py`**: draws the three icons with `zlib` and `struct` only (a rising line on a dark square, inside the maskable safe zone) and writes them as PNG.
3. **Python side:** the codes the UI localizes become constants where they were literals (`backtest.FLAGS`, `rules.ISSUE_CODES`, `exits.REASONS`, `jobs.PHASES` and `jobs.OUTCOMES`, `optimize.PHASES` and `optimize.WINDOW_STATUSES`, `web.HTTP_CODES`); `GET /api/strategies` adds each market's defaults (capital, benchmark, costs) from `markets.py`, so the form is prefilled without copying §4.1 into JavaScript; the static handler names the content types of `.js`, `.css`, `.json`, `.webmanifest`, `.png` and `.html` itself, because Windows' registry can map `.js` to `text/plain`, which browsers refuse for modules.
4. **Tests** (`tests/test_static.py`, part of AC-21):
   - both locale files have the same keys, and they cover every code collected from the Python constants (and every `ApiError` code in `web.py`), so later prompts extend the code, not a list in the test;
   - the static files outside `vendor/` contain no `innerHTML`, `insertAdjacentHTML`, `document.write`, script element without `src`, `style` attribute or inline event handler, and no URL of another host;
   - the manifest is valid JSON with `start_url`, `scope` and `display`, and its icons, like the apple-touch icon, exist as PNG of the stated sizes;
   - the ECharts file matches its SHA-256.
5. Run `.venv/bin/python -m pytest -q` until it passes, and write the manual checklist below into this plan's Result.

### Readings where the spec is silent

- **The market defaults travel with `GET /api/strategies`** (one more field), because the Backtest view loads it anyway; §19's table has no route for them.
- **The account kind** comes from `app.services.ib.account_kind` once prompt 21 reports it; until then trade mode shows TRADE without a kind.
- **The rule builder's operand list** comes from `GET /api/indicators`, filtered by the rule's timeframe as §9.6 allows.

## 2. Extension points

- **A view** is one module with `render(root, context)`; the tab bar lists it.
- **A code** is a locale key under its prefix; the test finds new codes in the Python constants.

## 3. Refactor seams

- Every request goes through `lib/api.js`, every string through `lib/i18n.js`, every element through `lib/dom.js`.
- Views read the state document from the shell and never poll on their own.

## 4. Deferred items (appended to TODO.md)

- None.
