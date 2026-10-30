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

## Result

- **The AC-21 checks of this prompt pass.** `.venv/bin/python -m pytest -q`: 1141 passed (also with `-X dev -W error`). `tests/test_static.py` collects the codes from the Python constants and from the codes `web.py` and `optimize.py` raise, and also checks that every literal `t("…")` key the UI asks for exists.
- Built as planned. Every module also parses under Node's ES-module checker, and a scratch smoke run here (headless Chromium against the web layer over temporary stores, not `python -m btlab`) logged in, opened every view at 375 px and 1280 px without a page error, and ran a backtest from the form in zh-TW through to its BACKTEST_SAVED event on Home. That run found two bugs, fixed before this commit: a duplicate function name in `views/data.js`, and a wide table that widened the whole page on a phone instead of scrolling inside its container.
- Full-page screenshots draw the sticky header and tab bar where the page was scrolled; on a device they stay at the top and the bottom.

### Manual checklist for the owner (on the PC and the devices)

Devices:
- [ ] Edge on the PC: open `http://127.0.0.1:8091`, install it as an app (Edge menu → Apps → Install), and reopen it from the Start menu: it opens without the address bar.
- [ ] Edge in light and dark (Windows Settings → Personalization → Colors): text, borders and the badge stay legible in both.
- [ ] Both languages: switch between 中文 and English in the header and in Settings; every label changes, codes (outcomes, phases, event kinds) read as words, and the choice survives a restart.
- [ ] iPhone at 375 px, portrait, through Tailscale Serve (HTTPS): no horizontal page scroll on any view, tables scroll inside their own frames, the bottom tab bar clears the home indicator, every button is easy to hit with a thumb.
- [ ] iPad through Tailscale Serve, portrait and landscape: the side bar appears from 900 px, the bottom tab bar below that.
- [ ] Add to the home screen on the iPhone and the iPad (Share → Add to Home Screen): the icon is the green rising line on dark blue, and the app opens standalone.

Login and logout:
- [ ] A wrong token waits about a second, then says the token is wrong; the right one opens Home.
- [ ] Log out from the header and from Settings: the login form returns, and a reload stays on it.
- [ ] The badge says TEST in test mode; in trade mode it says TRADE (with paper or live once IB reports the account), and a live account adds the red banner.

Lists, symbols and updates:
- [ ] Create a US list and a TW list; a duplicate name says the name is in use.
- [ ] Add symbols (lower case is fine), reorder them with ↑ and ↓, remove one with ×, rename the list, delete a list (with its confirmation).
- [ ] A bad symbol is marked at the "Add symbols" field.
- [ ] In the symbols table: override a category, switch minute bars on and off, purge an unlisted symbol (with its confirmation); purging a listed one says it is in use.
- [ ] Press Update US: Home and Data show the job, its phase, the symbol being fetched and the progress, every 2 s; when it ends, the event list shows the outcome and the symbols table refreshes. A second press while it runs says a job is running. Cancel works from Home.

The backtest form:
- [ ] A built-in (for example MA_CROSS): change the market and see the costs, capital and benchmark switch to that market's defaults; the parameter form follows the strategy; steppers respect each parameter's range.
- [ ] A plugin: the example plugins appear, and a broken plugin file in `strategies/` is listed with its error after Settings → reload or a restart.
- [ ] A rule: pick a saved rule, or open the rule builder and use its rule; the timeframe follows the rule.
- [ ] Invalid input (empty name, start after end, fast not below slow) is shown at the field itself, and anything without a field is listed under the form.
- [ ] A valid run says it is running, and BACKTEST_SAVED appears on Home.

The rule builder:
- [ ] Build an entry with ALL, ANY and NOT groups; the "Add group" button disappears at the third level.
- [ ] Choose an operand that does not fit the timeframe (for example a minute feature on a minute rule, or VWAP on a daily rule): the error appears at that operand within a moment.
- [ ] Refer an argument to a parameter, then rename or delete the parameter: the unknown-parameter error appears at the argument.
- [ ] Save under a name, change it, save again; a second rule with the same name says the name is in use; load it back from "Saved rules"; the JSON view shows the rule.
