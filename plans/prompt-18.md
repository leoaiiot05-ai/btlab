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
