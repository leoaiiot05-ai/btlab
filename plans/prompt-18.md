# Prompt 18: Web UI: results, charts, comparison and optimization

Covers SPEC.md §12, §13, §19 and §20 (items 5 and 6 of §20.2, and §20.3). Acceptance: AC-21.

## 1. Steps

1. **`lib/charts.js`**: loads the vendored ECharts once, on the first chart (the login page does not need it); every chart uses the canvas renderer, `renderMode: 'richText'` tooltips, a time axis (days or epoch seconds from the payload's `time_unit`), `dataZoom` inside (pinch and drag) and a slider, range buttons (1M, 6M, 1Y, 5Y, All on daily runs; 1D, 5D, 1M on minute ones), `sampling: 'lttb'` on lines, and the light or dark theme of `prefers-color-scheme`, switching with it.
2. **`lib/colors.js`**: the chart colours; green is up and red is down (§20.3, intentional), for candles, return heatmaps, annual bars and markers; every coloured value also carries its sign or label.
3. **Results (`views/results.js`, item 5):**
   - the list, filtered by market and by name, with key metrics, flags and the keep-offline mark; boxes to choose 2 to 4 results for comparison;
   - a result's page: SURVIVORSHIP as a quiet note and every other flag as a banner with its detail; metric cards with the strategy next to the benchmark; the charts: equity against the benchmark with a log-axis toggle, drawdown, the monthly-return heatmap (labelled values), annual returns (signed bars), exposure (gross and net), and candles with trade markers for a chosen symbol, loaded in windows of at most 5,000 bars with "earlier" to load the previous window; the trades table, paged; actions: rename, keep offline, the two CSV exports, delete (with confirmation and IN_USE), compare;
   - the comparison (`#compare/1,2,3`): the curves rescaled to 1 and overlaid, and the metrics side by side.
4. **Optimize (`views/optimize.js`, item 6):** the base configuration (a saved backtest, or the Backtest view's current form); a range per strategy parameter prefilled from its schema (min, max and step, or the choices), plus any other path; objective, minimum trades and mode; the live count of combinations against the 2,000 limit; run, with the estimate and its confirmation (409 NEEDS_CONFIRMATION) and the progress from the state document; the saved optimizations with the combinations table sortable by any column, the heatmap over two chosen paths, the in-sample against out-of-sample metrics, the walk-forward windows with the chained curve, promote per combination or window, and delete.
5. **Shell:** routes with an argument (`#results/12`, `#compare/1,2`); the Backtest view keeps its current form for the Optimize view.
6. **Tests** (`tests/test_static.py`): the locale and static checks cover the new files (they already scan every file and every literal key); the chart colour constants make up green and down red.
7. Run `.venv/bin/python -m pytest -q`, smoke-test the views in headless Chromium against temporary stores (CSP violations included), and extend the manual checklist.

### Readings where the spec is silent

- **ECharts loads on the first chart**, as a classic script from `vendor/`, so the login page stays light; the CSP needs no exception if the smoke run shows no violation.
- **The heatmap's colours** diverge around 0: green above, red below, with each cell's value written in it.
- **Optimize's ranges** cover the strategy's parameters by default; another path (an exit, the sizing) is one more row.

## 2. Extension points

- **A chart** is one option builder in a view over `lib/charts.js`.
- **A result action** is one button; "use for a live portfolio" and "write to ibguard" join with prompts 22 and 23.

## 3. Refactor seams

- Time conversion, zoom and range buttons live in `lib/charts.js`; views build series only.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-21 holds.** `.venv/bin/python -m pytest -q`: 1148 passed (also with `-X dev -W error`). The static checks scan the new files and keys; `test_the_chart_colours_make_up_green_and_down_red` checks the constants and the candle colours.
- Built as planned. ECharts renders under the CSP of §21 unchanged: a headless Chromium smoke run of the results list, a result page, a comparison of 3 runs, the optimizations list and an out-of-sample optimization, at 1280 px light and 375 px dark, logged no page error and no CSP violation, so no `style-src` exception was needed.
- That run found three things, fixed here: the maximum drawdown read as a green gain (it is stored positive; it now reads as a red loss), an entry marker of a trade held through a split sat at half its candle (a split rescales the trade's average; the candles route now shows the entry as filled), and crowded heatmap labels on phones (compact signed numbers there).
- The shell now hands views the shared context, so the rule builder's rule really reaches the Backtest view, and the Backtest view's current form reaches Optimize.

### Manual checklist, continued (on the PC and the devices)

Results on each device (Edge on the PC, the iPhone at 375 px, the iPad both ways):
- [ ] The list filters by market and by name; the flags show in words; a kept-offline result is marked.
- [ ] A result page shows SURVIVORSHIP as a quiet note and any other flag as a banner with its detail (a run of a peeking plugin shows LOOKAHEAD_SUSPECT with its time and symbol).
- [ ] Metric cards: the strategy next to its benchmark; gains green with +, losses red with −, the maximum drawdown as a red −.

Charts:
- [ ] Zoom by pinch on the iPhone and iPad, by dragging on the PC, and with the slider; the range buttons (1M, 6M, 1Y, 5Y, All) set the window.
- [ ] The log-axis toggle on the equity chart.
- [ ] Tooltips on tap (phone) and on hover (PC), with values in words and signs.
- [ ] The monthly heatmap: green months positive, red negative, each with its signed number.
- [ ] Switch Windows or iOS between light and dark while a result is open: the charts follow.

Candles:
- [ ] A daily run: candles green when they close up and red when down; ▲ entries and ▼ exits sit on the bars, also for a position held through a split.
- [ ] A minute run (once IB minute bars exist, prompt 21): candles on the run's timeframe with the markers; "Earlier bars" loads the window before.

Comparison:
- [ ] Choose 4 results in the list and press Compare: 4 curves starting at 1, and their metrics side by side.

Optimization:
- [ ] Start from a saved backtest and from the current Backtest form; the ranges come prefilled from the schema; the count updates as ranges change and warns above 2,000.
- [ ] A long run asks for confirmation with its estimate in minutes; the progress shows while it runs.
- [ ] A grid: sort the table by each column; the best is starred; the heatmap over two parameters; promote a row and find the new backtest in Results.
- [ ] An out-of-sample run: in-sample against out-of-sample metrics and the split day.
- [ ] A walk-forward run over at least 4 years: the windows table, NO_RESULT windows in cash, the chained curve and its metrics.

Exports:
- [ ] Export trades CSV and equity CSV from a TW result whose symbols have Chinese names, and open them in Excel: the columns of §12, Chinese text intact, times in the market's time zone.
