# btlab specification (v1)

This is the agreed behaviour. Behaviour marked **intentional** is the owner's explicit choice and must not be changed without asking, even where it looks like a bug.

## 1. Purpose and scope

btlab is the owner's stock backtesting lab. One Python process runs on the owner's Windows 11 PC. It:

1. downloads daily bars and corporate actions for US stocks and ETFs (Tiingo) and Taiwan stocks and ETFs (FinMind), the 3-month T-bill rate (FRED), and 1-minute bars for chosen symbols (Interactive Brokers through `ib_async`) (§5, §6);
2. backtests strategies on one market at a time: ten built-in strategies, Python plugins written by the owner, and rules the owner assembles in a visual rule builder, on daily or minute bars, long and short, with exits, position sizing, costs, taxes and corporate actions (§8 to §11);
3. reports the results with metrics, tables and charts, stores every run, and compares runs (§12);
4. optimizes parameters with a grid search, an out-of-sample check and walk-forward analysis (§13);
5. runs one live portfolio per market from a saved backtest: in test mode it records virtual orders and a paper-trading curve; in trade mode it sends the orders the owner confirms to IB (§14, §15);
6. coordinates with ibguard, the owner's exit engine: it writes the take-profit and stop-loss chosen in a backtest, and it lifts and restores ibguard's protection around its own sell orders (§16);
7. imports the leading-indicator series of spylead, the owner's research tool, as external indicators (§5.6);
8. serves a web UI, installable as a PWA: Edge on the PC ("Install as app") and Safari on iPhone and iPad through Tailscale Serve ("Add to Home Screen"). Saved results stay viewable offline (§20).

In scope: one user, one process, one market and one currency per backtest, the owner's own symbol lists.

Out of scope: everything in `TODO.md`. Notably mixed US and Taiwan portfolios, futures, options, crypto and FX, delisted symbols and historical index members, Monte Carlo analysis, live trading of minute strategies, and native apps.

## 2. Architecture

One Python process. aiohttp serves the JSON API and the static UI on one asyncio loop. Backtests and optimizations run in a process pool, so the loop stays responsive. IB is reached only through `ib_async`, on demand.

```
Edge on the PC (http://127.0.0.1:8091)        Safari on iPhone/iPad (https, Tailscale Serve :10000)
                       \                          /
             web (aiohttp, 127.0.0.1 only: JSON API + static PWA + service worker)
                                    |
      jobs (lanes: DATA, MINUTE, COMPUTE, LIVE; scheduler for automatic updates)
        |            |               |                 |                      |
     sources       store         process pool       live / trading          clients
 (Tiingo, FinMind, (SQLite:       backtest, optimize  (pure live core +      (spylead API,
  FRED)            btlab.db,      (pure core)         order orchestration)    ibguard API)
                   minute.db)                               |
                                                    ib_link / ib_orders --- ib_async --> IB Gateway
```

Starting layout. Deviate only with a reason stated in the plan.

| Path | Responsibility |
|---|---|
| `btlab/__main__.py` | CLI, wiring, lifecycle (§22, §23) |
| `btlab/config.py` | Load and validate `config.toml` (§22) |
| `btlab/markets.py` | Market constants: currency, time zone, sessions, defaults (§4). Pure. |
| `btlab/taxes.py` | Load `taxes.toml`; tax-rate lookup by market, category, date and day-trade flag (§10.6). Pure after loading. |
| `btlab/store.py` | SQLite persistence: `data/btlab.db` and `data/minute.db` (§18) |
| `btlab/http.py` | Shared request rules for the HTTP clients: timeouts, retries, pacing, redaction (§5.7) |
| `btlab/tiingo.py`, `btlab/finmind.py`, `btlab/fred.py` | Data clients and pure payload translation (§5) |
| `btlab/prices.py` | Corporate-action normalization, adjusted series, alignment to a calendar (§6). Pure. |
| `btlab/calendar.py` | Trading days, sessions, tradability (§4.3, §6.5). Pure. |
| `btlab/indicators.py` | Indicators (§8.1). Pure. |
| `btlab/features.py` | Minute-derived daily features and today-so-far features (§8.2, §8.3). Pure. |
| `btlab/sizing.py` | Position sizing (§9.4). Pure. |
| `btlab/builtins.py` | Built-in strategies A to J (§9.3). Pure. |
| `btlab/plugins.py` | Plugin loading, parameter schemas, the look-ahead check (§9.5, §9.7) |
| `btlab/rules.py` | Rule-builder schema, validation and evaluation (§9.6). Pure. |
| `btlab/costs.py` | Commissions, taxes, slippage, borrow fees, short-sale fees (§10.6). Pure. |
| `btlab/exits.py` | Exit overlays and intrabar paths (§10.8). Pure. |
| `btlab/engine.py` | The simulation loop and portfolio accounting (§10). Pure. |
| `btlab/intraday.py` | Minute timelines: resampling, sessions, close-out, PDT flag (§11). Pure. |
| `btlab/metrics.py` | Metrics, tables, benchmark (§12). Pure. |
| `btlab/backtest.py` | One run: config to data views to engine to metrics to result (§10.1). Pure given loaded data. |
| `btlab/optimize.py` | Grid, out-of-sample check, walk-forward (§13); the pool is injected |
| `btlab/live.py` | Live-portfolio core: targets, diffs, order proposals, virtual fills, ledger (§14). Pure. |
| `btlab/trading.py` | Order orchestration in trade mode: limits, sending, monitoring, recovery, ibguard protocol (§15, §16) |
| `btlab/ib_link.py` | ib_async adapter: connection, historical bars, positions, contract details, market rules (§5.4, §15) |
| `btlab/ib_orders.py` | ib_async adapter for orders. Trade mode only; the only module that places or cancels orders (§15) |
| `btlab/ibguard_client.py`, `btlab/spylead_client.py` | JSON-API clients for ibguard and spylead (§5.6, §16) |
| `btlab/notifier.py` | Optional SMTP notifications (§17.6) |
| `btlab/jobs.py` | Job lanes, data updates, backtest and optimization jobs, minute backfill, live jobs (§17) |
| `btlab/scheduler.py` | Automatic-update schedule and catch-up decisions (§17.4). Pure. |
| `btlab/web.py` | aiohttp app (§19) |
| `btlab/bench.py` | Timing on production-size synthetic data (§24.3) |
| `btlab/static/` | `index.html`, `app.js`, `style.css`, `sw.js`, `manifest.webmanifest`, `icons/`, `locales/zh-TW.json`, `locales/en.json`, `vendor/echarts/` (§20) |
| `strategies/` | The owner's plugins, plus one example of each style (§9.5). Committed. |
| `taxes.toml` | The Taiwan tax table (§10.6). Committed. |
| `scripts/` | `install-autostart.ps1`, `make_icons.py` (§20, §23) |
| `run.bat` | Start the app from the venv in a console |
| `tests/` | Tests and fakes: HTTP fakes, a fake IB, fake ibguard and spylead servers, a clock, a synchronous pool |
| `buildctl/` | Build automation (baton and lease of the build). Not part of the app; never edit it. |

## 3. Terms

| Term | Meaning |
|---|---|
| market | `US` or `TW`. A backtest, a list and a live portfolio each belong to one market. |
| day | A calendar date. Internally an integer: days since 1970-01-01. |
| session | One regular trading session of a market (§4.2). Daily bars have one bar per session. |
| bar | One OHLCV record: a session (daily) or a minute interval. Minute bars are stamped with the UTC epoch second of their start. |
| timeframe | `1d`, `1m`, `5m`, `15m`, `30m` or `60m`. Only `1d` and `1m` are stored; the others are built from `1m` (§11.1). |
| raw prices | Prices as traded, unadjusted. |
| adjusted prices | Raw prices multiplied by the cumulative back-adjustment factor, so the latest price equals the raw price and returns include dividends (§6.3). |
| price mode | `REALISTIC` (default): signals on adjusted prices, fills, valuation and costs on raw prices with corporate actions applied to the position. `ADJUSTED`: everything on adjusted prices. **Intentional**: both are offered (§10.2). |
| fill timing | `NEXT_OPEN` (default): a signal at a bar's close fills at the next tradable bar's open. `SAME_CLOSE`: it fills at the same bar's close (optimistic, labelled so). |
| target | A strategy's instruction for one symbol at one bar: a direction, a weight or a quantity (§9.1). |
| path | The ordered prices a bar is assumed to pass through, used for intrabar exits (§10.8). |
| category | A symbol's tax class: `STOCK`, `ETF`, `BOND_ETF` or `TDR` in TW; `STOCK` or `ETF` in US. |
| live portfolio | The one portfolio per market and mode that follows a saved daily backtest (§14). |
| run mode | `test` (default): live orders are virtual. `trade`: confirmed orders go to IB. Set in the config file. |
| account kind | `paper` or `live`: which IB account IB Gateway is logged in to (account id starting with `D` is paper). |
| batch | The orders a live portfolio proposes for one session (§14.4). |
| in-sample series | spylead's F and composite: fitted over their whole history, so backtests that use them are optimistic (§5.6). |

## 4. Markets

### 4.1 Constants

| | US | TW |
|---|---|---|
| Currency | USD | TWD |
| Time zone | America/New_York | Asia/Taipei |
| Regular session | 09:30–16:00 | 09:00–13:30 |
| Daily bars and corporate actions | Tiingo | FinMind |
| Minute bars | IB (`ib_async`) | IB (`ib_async`) |
| Trading days | The days of stored SPY bars; weekdays after the last stored day | FinMind `TaiwanStockTradingDate` |
| Default benchmark | SPY | 0050 |
| Default initial capital | 100,000 | 1,000,000 |
| Backtest lot | 1 share | 1 share (odd lots); option "board lots only": 1,000 shares |
| Price limit | none | ±7% before 2015-06-01, ±10% from 2015-06-01 |
| Default commission | USD 0.005 per share, minimum USD 1.00, maximum 1% of trade value (IBKR Pro Fixed) | 0.08% of trade value, minimum TWD 80 (IB Taiwan, tier I) |
| Transaction tax | none | `taxes.toml`, sell side (§10.6) |
| Default slippage | 5 bps | 10 bps |
| Short collateral from own cash | 50% of short value | 90% of short value (融券保證金) |
| Short costs | borrow fee 0.5% a year (per-symbol override), dividends paid on ex-date | short-sale fee 0.08% of the sale (融券手續費), forced cover (§10.7) |
| Default dividend withholding | 30% | 0% |
| Automatic update (Asia/Taipei) | Tuesday–Saturday 08:30 | Monday–Friday 18:00 |
| Live order kinds | MOO (market on open) or limit, day | Limit, day |

- SPY and 0050 are always fetched, whatever the lists contain: SPY defines the US trading days and both are default benchmarks.
- Every default above can be changed per backtest in the UI except the time zone, sessions, trading days, price limits and short-collateral rates. **Intentional.**
- Initial data window: the first update of a market fixes `data_start` = that day minus 31 years (30 years plus one year of indicator warm-up). It is stored and never changes.

### 4.2 Sessions

- Regular sessions only. Minute bars outside the regular session are never requested or stored.
- A day's session ends at the regular close, or earlier on early-close days. The actual end is the end of the day's last stored minute bar; without minute bars the regular close applies.
- Session times convert to UTC through `zoneinfo` (Windows needs `tzdata`).

### 4.3 Calendars

- TW: the trading days are FinMind's `TaiwanStockTradingDate`, refreshed on every TW update.
- US: the trading days are the days of stored SPY bars. Days after the last stored SPY bar count as trading days when they are weekdays; this matters only for scheduling and for the next live session, and a holiday then simply produces no new data.
- A backtest aligns every symbol to its market's calendar (§6.4).

## 5. Data sources

Tests never reach any of these services: every client takes an injected session or transport, and tests use fakes (§24.1).

### 5.1 Tiingo (US daily bars)

- Authentication: header `Authorization: Token <key>`. The key never goes in a URL.
- Prices: `GET https://api.tiingo.com/tiingo/daily/<ticker>/prices?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&format=json&resampleFreq=daily`. Each row has `date` (ISO timestamp; use its date part), `open`, `high`, `low`, `close`, `volume`, `adjOpen`, `adjHigh`, `adjLow`, `adjClose`, `adjVolume`, `divCash`, `splitFactor`.
- Tickers: the owner writes class shares with a dot (`BRK.B`); Tiingo receives a hyphen (`BRK-B`).
- Stored: the raw OHLCV. `divCash > 0` gives a CASH_DIV action and `splitFactor ≠ 1` a SHARE_RATIO action with ratio `splitFactor` (§6.2). Tiingo's adjusted fields are not stored: btlab computes its own (§6.3).
- Windows: the first fetch requests `data_start` to today. Later fetches request from 7 days before the latest stored day to today and upsert those days, because Tiingo can revise recent rows.
- An unknown ticker (HTTP 404) fails that symbol with SYMBOL_UNKNOWN.
- Limits of the free plan: 50 requests per hour, 1,000 per day, 500 distinct symbols per month, internal use only. The client keeps itself to 45 requests per rolling hour and 900 per rolling day. The request log is stored (`settings`, §18), so a restart does not reset it. When the hourly budget is spent, the job waits, shows until when, and stays interruptible. When the month's distinct symbols exceed 450, an event TIINGO_SYMBOL_BUDGET warns.

### 5.2 FinMind (Taiwan daily bars and corporate actions)

- Requests: `GET https://api.finmindtrade.com/api/v4/data?dataset=<name>&data_id=<code>&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` with header `Authorization: Bearer <token>`. The response is `{"msg": ..., "status": 200, "data": [...]}`; any other `status` is an error whose text is FinMind's `msg`.
- Limits of a registered free account: 600 requests per hour. The client keeps itself to 550 per rolling hour, with the log stored like Tiingo's. FinMind is under maintenance on Sundays 00:00–07:00 Asia/Taipei: a request then fails with MAINTENANCE, and the scheduler retries after 07:00.
- Data are for non-commercial use, and the UI credits "FinMind".
- Datasets (field names checked against FinMind's documentation, October 2026). Translation ignores extra fields and fails with BAD_PAYLOAD when a needed field is missing.

| Dataset | Parameters | Fields used |
|---|---|---|
| `TaiwanStockTradingDate` | none | `date` |
| `TaiwanStockInfo` | none | `stock_id`, `stock_name`, `type` (`twse` or `tpex`), `industry_category`, `date` (keep each stock's latest row) |
| `TaiwanStockPrice` | `data_id`, dates | `date`, `open`, `max`, `min`, `close`, `Trading_Volume` (shares). Rows with `close = 0` are no-trade days. |
| `TaiwanStockDividendResult` | `data_id`, dates | `date` (ex-date), `before_price`, `after_price`, `stock_or_cache_dividend` (`息`, `權` or `權息`) |
| `TaiwanStockDividend` | `data_id`, dates | `CashEarningsDistribution`, `CashStatutorySurplus`, `CashExDividendTradingDate` |
| `TaiwanStockSplitPrice` | dates only; filter rows by `stock_id` | `date`, `stock_id`, `type`, `before_price`, `after_price` |
| `TaiwanStockParValueChange` | `start_date` only; filter by `stock_id` | `date`, `stock_id`, `before_close`, `after_ref_close` |
| `TaiwanStockCapitalReductionReferencePrice` | `data_id` (required on the free plan), dates | `date`, `ClosingPriceonTheLastTradingDay`, `PostReductionReferencePrice`, `ReasonforCapitalReduction` |
| `TaiwanStockMarginShortSaleSuspension` | `data_id`, dates | `date` (start of the short-sale suspension), `end_date`, `reason` |

- A TW update makes, in order: the calendar, the stock info (at most once a day), the split and par-value tables, then per symbol the price, dividend result, dividend policy, capital reduction and short-suspension requests. About 250 requests for 50 symbols.
- Windows: prices as in §5.1 (7-day overlap, upsert). Corporate-action datasets: from 400 days before the latest stored day, because policies and corrections arrive months ahead; the first fetch starts at `data_start`.

### 5.3 FRED (risk-free rate)

- `GET https://api.stlouisfed.org/fred/series/observations?series_id=DTB3&api_key=<key>&file_type=json&observation_start=YYYY-MM-DD`. Each observation has `date` and `value` (a string; `"."` means missing).
- FRED requires the key in the query string. No URL that contains it is ever logged or stored unredacted.
- DTB3 (3-month T-bill, secondary market, percent) is the risk-free rate of both markets. **Intentional**: the owner chose it for Taiwan too.
- Updated with every data update, from 30 days before the latest stored day.

### 5.4 IB (1-minute bars)

- Reached only through `btlab/ib_link.py`, behind a `HistoryPort` protocol that tests replace with a fake. No other module imports `ib_async`, except `ib_orders.py` (§15).
- Connection: `ib.host`, `ib.port` and `ib.client_id` from the config. The client id must differ from ibguard's (17); the default is 31. One connection serves history, positions and contract details: `readonly=True` in test mode; in trade mode `readonly=False`, and `ib_orders.py` shares it (§15).
- Request, one per symbol and session day, because IB serves at most one day of 1-minute bars per request: `await ib.reqHistoricalDataAsync(contract, endDateTime=<the session's end, UTC>, durationStr="1 D", barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, formatDate=2)`, wrapped in a 60 s timeout.
- Pacing: at least 2 s between requests (IB soft-throttles bars of 1 minute and longer). A pacing violation waits 60 s, then retries once.
- Contracts: US `Stock(<symbol with "." as " ">, "SMART", "USD")`; TW `Stock(<code>, <exchange>, "TWD")` with the exchange recorded for the symbol (§7). Resolve with `reqContractDetailsAsync`; only a unique match is used, and its `conId`, size increments and market-rule ids are cached in `contracts` (§18).
- Permissions: an error that means missing market-data permissions, no subscription, or an unknown contract marks the symbol-day NO_PERMISSION, stops further requests for that market until the next update, and the UI says so. IB does not offer Taiwan market access to residents of Taiwan, so TW minute bars may never be available (§25).
- Normalization to raw prices. IB returns split-adjusted bars. After fetching day d, compare the stored raw daily close of d with the last minute close of d (factor = daily ÷ minute):
  - within 2% of 1: store as returned;
  - within 2% of the product of the share ratios of the actions after d: multiply prices by that product, divide volumes by it, and store;
  - otherwise: store as returned, mark the day MISMATCH, and record an event. Intrabar paths then fall back to the daily path for that day (§10.8).
  - A day whose daily bar is not stored yet is skipped and retried later.
- Coverage is kept per symbol-day: OK, EMPTY (IB returned no bars, for example a halt), FAILED (retried later), NO_PERMISSION, MISMATCH.

### 5.5 Which symbols get minute bars

- Minute bars cost hours of requests, so they are fetched only for symbols the owner marks "minute bars" in a list, plus the symbols of active live portfolios (§17.3). **Intentional.**
- Default depth: 3 years back (`minute.backfill_years`). The backfill runs in the background, newest days first, and resumes where it stopped. Every data update also queues the sessions since the last covered day.

### 5.6 spylead (external indicators)

- `btlab/spylead_client.py` uses spylead's documented JSON API, never its database. Base URL and token from the config; an empty URL disables the import.
- Calls: `POST /api/login` with `{"token": ...}` (the session cookie lives in memory only); `GET /api/state`; `GET /api/charts/spy`; `GET /api/charts/series/{name}` for each series whose status is `KEPT`; `GET /api/charts/f` when `f.status` is `OK`.
- Series kept, each as `{first_day, values}` over calendar days in original units:

| btlab name | From | In-sample |
|---|---|---|
| `SPYL1` | `rt` of `/api/charts/spy` | no |
| `LEADL1_<name>` | `rt` of `/api/charts/series/{name}` | no |
| `F` | `f` of `/api/charts/f` | yes |
| `COMPOSITE` | `composite` of `/api/charts/f` | yes |

- A snapshot is taken after every data update when spylead is configured, and on demand. It replaces the stored set in one transaction. When spylead is unreachable, the previous snapshot stays and an event SPYLEAD_UNREACHABLE records it.
- Alignment: a bar on market date d takes the value of calendar day d. A spylead one-sided value for day d uses only data released by the last month end before d, so it is known on day d. Undefined values are NaN.
- F and COMPOSITE are fitted over their whole history. Every backtest or optimization that uses them carries the flag IN_SAMPLE_SERIES, shown as "in-sample, optimistic". **Intentional.**

### 5.7 Request rules

All HTTP clients (Tiingo, FinMind, FRED, spylead, ibguard) share `btlab/http.py`:

- One aiohttp ClientSession per job, passed in; a 30 s timeout per request.
- Retries: timeouts, connection errors and HTTP 5xx get 3 retries after 2, 4 and 8 s. HTTP 429 waits `Retry-After` (at most 60 s, default 60) and retries once. Other 4xx: no retry.
- Sleeping and the clock are injected, so tests do not wait.
- Errors are one `SourceError` type with a code (TIMEOUT, HTTP_<status>, BAD_PAYLOAD, MISSING_KEY, SYMBOL_UNKNOWN, RATE_LIMITED, MAINTENANCE, UNREACHABLE) and English text that never contains a URL, a key or a token.
- A redaction helper masks `api_key`, `token` and `Authorization` values in anything that reaches a log.
- A missing key fails every request of that source with MISSING_KEY, without a request.

## 6. Price data

### 6.1 Daily bars

- Stored raw: open, high, low, close, volume, per market, symbol and day.
- A TW no-trade day is stored with `no_trade = 1`, null prices and the reported volume.

### 6.2 Corporate actions

Each action is normalized to one of two kinds:

| Kind | Fields | Effect on a position in REALISTIC mode (§10.7) |
|---|---|---|
| CASH_DIV | `cash` per share | Cash credited to longs (less withholding) and debited to shorts on the ex-date |
| SHARE_RATIO | `ratio` r: new shares per old share | Quantity × r on the ex-date; price levels ÷ r |

Each action also keeps its `detail` (SPLIT, REVERSE_SPLIT, STOCK_DIV, PAR_CHANGE, CAPITAL_REDUCTION, DIV_RESULT) and, for TW dividend results, `ref_before` and `ref_after`.

US actions come from Tiingo (§5.1). TW actions:

1. **Dividend result** on day t, with P = `before_price` and A = `after_price`. The cash dividend D is the sum of `CashEarningsDistribution + CashStatutorySurplus` over the dividend-policy rows whose `CashExDividendTradingDate` is t.
   - `息` (cash only): CASH_DIV(D), or CASH_DIV(P − A) when no policy row matches.
   - `權` (stock only): SHARE_RATIO(P / A).
   - `權息` with D known: CASH_DIV(D) and SHARE_RATIO((P − D) / A).
   - `權息` without a policy row: SHARE_RATIO(P / A) and an event DIVIDEND_SPLIT_UNKNOWN. The value is preserved; only the split between cash and shares is approximated.
   - A share ratio within 1e-6 of 1 is dropped.
2. **Split or reverse split**: SHARE_RATIO(`before_price` / `after_price`).
3. **Par-value change**: SHARE_RATIO(`before_close` / `after_ref_close`).
4. **Capital reduction**: SHARE_RATIO(`ClosingPriceonTheLastTradingDay` / `PostReductionReferencePrice`). A cash refund is approximated as part of the share value (§25). **Intentional.**

A ratio or cash amount that is not finite, or a ratio ≤ 0, is skipped with an event BAD_ACTION.

### 6.3 Adjusted series

- Each event day t has a factor g_t:
  - US: `ref = C_prev / r − D` and `g = ref / C_prev`, where `C_prev` is the previous raw close, r the day's share ratio (1 without one) and D the day's cash dividend (Tiingo quotes it per post-split share).
  - TW dividend result: `g = A / P`. Other TW share events: `g = 1 / r`. Several events on one day multiply.
- Back-adjustment: `adj_price(τ) = raw_price(τ) × Π g_t` over the event days t after τ. `adj_volume(τ) = raw_volume(τ) × Π r_t` over the share events after τ.
- A factor outside (0, 2] is ignored with an event BAD_ACTION.
- Adjusted series are computed when a run loads its data and are never stored. **Intentional**: they always reflect every stored action.

Examples, computed by hand (use them as tests):

| Id | Case | Raw closes | Action | Adjusted closes |
|---|---|---|---|---|
| A1 | US cash dividend | 100 (d1), 99 (d2) | CASH_DIV 2 on d2 | 98, 99 |
| A2 | US 2:1 split | 100 (d1), 51 (d2) | SHARE_RATIO 2 on d2 | 50, 51; volume of d1 doubled |
| A3 | US split and dividend on one day | 100 (d1), 49 (d2) | SHARE_RATIO 2 and CASH_DIV 0.5 on d2 | 49.5, 49 |
| A4 | TW cash dividend (2330, 2019-06-24) | 248.5 (d1), 240.5 (d2) | DIV_RESULT P 248.5, A 240.5 | 240.5, 240.5 |
| A5 | TW stock and cash dividend | 50 (d1), 42 (d2) | `權息`, P 50, A 43.6364, D 2 | 43.6364, 42; ratio (50 − 2) / 43.6364 = 1.1 |
| A6 | TW reverse split (00632R, 2024-12-11) | 3.28 (d1), 22.96 (d2) | SHARE_RATIO 3.28 / 22.96 = 1/7 | 22.96, 22.96 |

### 6.4 Alignment

- A run's arrays are bars × symbols on the market's calendar, from the run's start minus its warm-up (§10.1) to its end.
- A symbol is NaN before its first stored bar and after its last.
- Inside that range, a missing day or a no-trade day carries the previous raw close into open, high, low and close with volume 0. Such a bar is not tradable.

### 6.5 Tradability

- A bar is not tradable for a symbol when it has no bar, is a no-trade day, has volume 0, or holds NaN.
- **TW limit lock.** A bar is locked when `open = high = low = close` and `|close / ref − 1| ≥ 0.9 × limit`, where the limit is 7% before 2015-06-01 and 10% from that day, and ref is the previous raw close times the day's factor g_t (§6.3), or the previous raw close without an event. **Intentional**: the 0.9 margin absorbs tick rounding of the limit price.
  - Locked up: buys and short covers do not fill on that bar. Locked down: sells and short sales do not fill.
- An order that cannot fill stays pending to the next bar on which it can (§10.5).

### 6.6 Minute bars

- `data/minute.db` holds 1-minute bars of the regular session in raw prices (after §5.4), per market, symbol and UTC start second, with the coverage table of §5.4.

## 7. Lists and symbols

- A list has a name, one market and an ordered set of symbols. A symbol can be in several lists.
- Symbols: US `^[A-Z][A-Z0-9]{0,5}(\.[A-Z])?$`, uppercased; TW `^[0-9]{4,6}[A-Z]?$`.
- Every symbol of every list of a market, plus that market's default benchmark, is updated with the market (§17.2). A new symbol is fetched from `data_start`.
- Removing a symbol from every list keeps its data. A purge, which the UI confirms, deletes its bars, actions, minute bars and contract.
- Categories:
  - TW, by rule: `BOND_ETF` when the code starts with `00` and ends with `B`; `ETF` when it starts with `00`; `TDR` when it has six digits and starts with `91`; otherwise `STOCK`.
  - US: `STOCK` unless the owner marks it `ETF`. US categories are informational.
  - The owner can override any category in the UI (`category_source = OWNER`).
- TW exchange: `TWSE` or `TPEX`, from `TaiwanStockInfo.type`.
- Per-symbol settings: "minute bars" (§5.5) and, for US, a borrow fee override (§10.7).
- **Survivorship.** Lists hold symbols that exist today. Every backtest carries the flag SURVIVORSHIP, and the UI says "only symbols that still exist; results are biased upward". Delisted symbols and historical index members are in TODO.md. **Intentional.**

## 8. Indicators and features

### 8.1 Indicators

Inputs and outputs are float64 arrays shaped bars × symbols. Every indicator is causal: its value at bar t uses bars up to and including t only. Values are NaN until enough data exist; a window that contains NaN gives NaN. Indicators run on adjusted bars in both price modes (§10.2).

| Name | Arguments (default) | Definition |
|---|---|---|
| PRICE | `src` (close) | The source series: open, high, low, close or volume |
| SMA | `n` (20), `src` | Mean of the last n values |
| EMA | `n` (20), `src` | α = 2 / (n + 1); seeded with the SMA of the first n values; then `e = α·x + (1 − α)·e_prev` |
| RSI | `n` (14) | Wilder. Changes of close; the first average gain and loss are the means over the first n changes, then `avg = (avg_prev·(n − 1) + x) / n`. `RSI = 100 − 100 / (1 + gain / loss)`; 100 when loss is 0 and gain is not, 50 when both are 0 |
| MACD | `fast` (12), `slow` (26), `signal` (9), `out` (macd, signal, hist) | `macd = EMA(fast) − EMA(slow)` of close; `signal` = EMA of macd seeded with the SMA of its first `signal` defined values; `hist = macd − signal` |
| BBANDS | `n` (20), `k` (2), `out` (upper, middle, lower, pctb, width) | middle = SMA; sd = population standard deviation over n; upper and lower = middle ± k·sd; `pctb = (close − lower) / (upper − lower)`; `width = (upper − lower) / middle` |
| ATR | `n` (14) | True range `max(H − L, |H − C_prev|, |L − C_prev|)`, H − L on the first bar; seeded with the mean of the first n, then Wilder |
| DONCHIAN | `n` (20), `out` (upper, lower, middle) | upper = highest high of the n bars **before** t; lower = lowest low of the n bars before t; middle = their mean. **Intentional**: excluding bar t makes `close > upper` a breakout |
| HIGHEST, LOWEST | `n` (20), `src` (high, low) | Over the last n bars including t |
| ROC | `n` (20), `src` | `x_t / x_(t−n) − 1` |
| ZSCORE | `n` (20), `src` | `(x − SMA) / sd` with the population sd; NaN when sd is 0 |
| STDEV | `n` (20), `src` | Population standard deviation |
| VOLATILITY | `n` (60) | Sample standard deviation of the last n log returns of close, × √P, with P = 252 on the daily timeline and 252 × bars per regular session on minute timelines |
| VWAP | none | Minute timelines only: session VWAP from the session's first bar to t, of the typical price `(H + L + C) / 3` weighted by volume |

### 8.2 Minute-derived daily features

On the daily timeline, these use the day's minute bars, scaled by the day's adjustment factor so they match the adjusted daily series. A day without minute coverage OK gives NaN. They are known at the session's close.

| Name | Arguments (default) | Definition |
|---|---|---|
| LAST_N_MIN_RETURN | `n` (30) | Session close ÷ close of the minute bar that ends n minutes before the session end − 1 |
| FIRST_N_MIN_RETURN | `n` (30) | Close of the minute bar that ends n minutes after the open ÷ session open − 1 |
| CLOSE_VS_VWAP | none | Session close ÷ session VWAP − 1 |
| RANGE_POSITION | none | `(close − low) / (high − low)` of the session; 0.5 when high = low |
| RECLAIM_PREV_LOW | none | 1 when the session's low is below the previous session's low and its close is above it, else 0 |

### 8.3 Today-so-far features

On minute timelines: TODAY_OPEN, TODAY_HIGH, TODAY_LOW, TODAY_VWAP, TODAY_RETURN (`close / TODAY_OPEN − 1`) and MINUTES_SINCE_OPEN, each from the current session's bars up to t.

### 8.4 Daily values on minute timelines

An indicator computed on daily bars and used on a minute timeline takes the value of the last completed session before the bar's session. **Intentional**: today's daily bar is not known until the close.

### 8.5 External series

spylead series (§5.6) are indicators named as in §5.6, aligned by calendar day on every timeline.

### 8.6 Registry

One registry lists every indicator, feature and external series: its arguments with type, default and range, its outputs, and the timelines where it is valid. Validation, the rule builder and `GET /api/indicators` all read it.

## 9. Strategies

### 9.1 Contract

A strategy is evaluated at each bar's close, using data up to that close, and produces targets that fill according to the fill timing (§10.5). There are three output kinds:

| Kind | Shape | Meaning |
|---|---|---|
| DIRECTION | bars × symbols of +1, 0, −1 or NaN | +1 long, −1 short, 0 flat, NaN no instruction. The engine sizes each entry (§9.4). A change of direction enters, exits or reverses. |
| WEIGHTS | bars × symbols of signed weights or NaN, and an optional rebalance mask per bar | A weight is a fraction of equity. On a bar of the rebalance mask, every non-NaN target executes; on other bars only targets that changed by more than 1e-9 from the last executed one. |
| ORDERS | none: `on_bar(ctx)` calls the order functions of §9.5 | Event-driven |

- Negative directions or weights act only when the backtest allows shorting. Otherwise they count as 0 and the result carries the flag SHORTS_IGNORED.
- Built-ins, rules and plugins see adjusted bars (§10.2).

### 9.2 Parameter schema

Shared by built-ins, plugins and rules: a mapping from name to `{type: int | float | bool | choice, default, min, max, step, choices, label_en, label_zh}` (min, max and step for numbers; choices for choice). Unknown names, wrong types and values out of range are rejected with INVALID_FIELD and the JSON path. The UI builds its forms from the schema, and optimization ranges default to min, max and step.

### 9.3 Built-in strategies

| Id | Name | Output | Parameters (default, range) | Rule at each close |
|---|---|---|---|---|
| A | MA_CROSS | DIRECTION | `fast` (20, 2–200), `slow` (50, 5–400), `ma` (SMA or EMA), `allow_short` (false) | +1 while MA(fast) > MA(slow); otherwise −1 when allowed, else 0. `fast` must be below `slow`. |
| B | RSI | DIRECTION | `n` (14, 2–100), `lower` (30, 1–50), `upper` (70, 50–99), `allow_short` (false) | Long: enter when RSI < lower, exit when RSI > upper. Short, when allowed: enter when RSI > upper, exit when RSI < lower. |
| C | MACD | DIRECTION | `fast` (12), `slow` (26), `signal` (9), `allow_short` (false) | +1 while macd > signal; otherwise −1 when allowed, else 0 |
| D | BOLLINGER | DIRECTION | `n` (20, 5–200), `k` (2.0, 0.5–4), `allow_short` (false) | Long: enter when close < lower, exit when close > middle. Short: enter when close > upper, exit when close < middle. |
| E | BREAKOUT | DIRECTION | `entry_n` (20, 5–250), `exit_n` (10, 2–250), `allow_short` (false) | Long: enter when close > DONCHIAN(entry_n).upper, exit when close < DONCHIAN(exit_n).lower. Short mirrored. |
| F | BUY_HOLD | WEIGHTS | none | On the first bar of the run, equal weights over the symbols that have data; then hold |
| G | DCA | ORDERS | `amount` (US 1,000; TW 30,000), `frequency` (monthly or weekly) | On the first bar of each period, buy `amount / N` worth of each of the N symbols that have data, while cash covers it with costs. Never sells. |
| H | MOMENTUM_ROTATION | WEIGHTS with rebalance | `lookback` (126, 20–504), `skip` (0, 0–63), `top_k` (3, 1–20), `rebalance` (monthly or weekly), `weighting` (equal or vol_inverse), `abs_filter` (true) | On the first bar of each period: score = ROC(lookback) measured at bar t − skip; select the top_k symbols with a score, and with a score above 0 when `abs_filter`. Equal: 1/top_k each. vol_inverse: weights ∝ 1/VOLATILITY(60) over the selected, scaled to selected/top_k. Unfilled slots stay in cash. Long only. |
| I | DUAL_MOMENTUM | WEIGHTS with rebalance | `lookback` (252, 20–504), `safe` (a symbol of the run, or empty for cash), `rebalance` (monthly) | On the first bar of each month: the risk symbol (all but `safe`) with the highest ROC(lookback). If that ROC is above 0 and above the compounded risk-free return over the same bars: 100% in it. Otherwise 100% in `safe`, or cash. Long only. |
| J | ZSCORE_REVERSION | DIRECTION | `n` (20, 5–200), `entry_z` (2.0, 0.5–4), `exit_z` (0.5, 0–3), `allow_short` (false) | Long: enter when z < −entry_z, exit when z > −exit_z. Short: enter when z > entry_z, exit when z < exit_z. |

- Periods are calendar months or ISO weeks. "The first bar of a period" is the first bar whose date falls in a new period. **Intentional**: it needs no knowledge of future trading days, so backtests and live portfolios agree.
- In stateful rules (B, D, E, J), an exit and an entry of the opposite side on the same bar reverse the position.

### 9.4 Position sizing

DIRECTION outputs are sized at each entry:

| Mode | Parameters (default) | Entry quantity |
|---|---|---|
| FIXED_FRACTION (default) | `fraction` (1/N, N = symbols in the run) | `fraction × equity / price` |
| FIXED_AMOUNT | `amount` | `amount / price` |
| FIXED_SHARES | `shares` | `shares` |
| VOL_RISK | `risk_pct` (1.0), `atr_n` (20), `atr_mult` (2.0) | `risk_pct / 100 × equity / (atr_mult × ATR)` |

- Equity is the equity at the signal bar's close; price is that bar's close. In REALISTIC mode both are raw, and the ATR is converted to the raw scale with `raw_close / adj_close` at the signal bar.
- Quantities round down to the lot (§10.4). Each entry is capped at `max_position_pct` of equity (default 100).
- After entry the quantity is held until the direction changes; there is no drift rebalancing. **Intentional.**
- Rotation strategies (H, I) size through their own weighting. vol_inverse uses the 60-bar volatility, and its weights sum to 100% over the selected symbols (owner default).

### 9.5 Plugins

- Files in `strategies/` whose names do not start with `_`, loaded at startup and on reload (`POST /api/strategies/reload`). The web API has no route that writes code. **Intentional**: no remote code execution.
- A plugin module defines `NAME_EN`, `NAME_ZH`, `TIMEFRAME` (`1d` or a minute timeframe), `PARAMS` (§9.2), and exactly one of:
  - `signals(bars, params)`: vectorized. Returns a targets array (bars × symbols), WEIGHTS by default or DIRECTION when the module sets `OUTPUT = "direction"`, optionally with a rebalance mask as a second array.
  - `on_bar(ctx)`: event-driven.
- `bars` exposes read-only NumPy views: `symbols`, `times` (day integers or epoch seconds), `session` (session index per bar), `open`, `high`, `low`, `close`, `volume` (adjusted, bars × symbols), `tradable`, `rf` (risk-free rate per bar), `daily` (on minute timelines, the daily bars aligned as in §8.4) and `ext(name)` (an external series, aligned).
- `ctx` exposes `t`, `time`, `symbols`, `params`, `state` (a dict kept across bars), `history(field, n)` (the last n rows up to and including t, read-only), `position(symbol)`, `equity`, `cash`, and the orders `order_target_weight(symbol, w)`, `order_target_qty(symbol, q)`, `order_qty(symbol, dq)`, `order_value(symbol, amount)`. Orders fill according to the fill timing; the last order for a symbol in a bar wins.
- Plugin code runs in the pool workers. It is the owner's code and trusted, but it only ever receives `bars`, `params` and `ctx`: never the store, the config, IB or a network client.
- A plugin that fails to load (syntax error, missing attribute, invalid `PARAMS`, both functions or neither) is listed with its error by `GET /api/strategies`; the others still load.
- Two examples are committed: `strategies/example_vector.py` (an MA cross returning weights) and `strategies/example_event.py` (a breakout using `on_bar`).

### 9.6 Rule builder

A rule is JSON, interpreted by `rules.py`; it never executes code. **Intentional.**

```json
{
  "version": 1,
  "timeframe": "1d",
  "side": "long",
  "params": {"n": {"type": "int", "default": 20, "min": 5, "max": 100, "step": 5}},
  "entry": {"all": [
    {"left": {"ind": "SMA", "args": {"n": {"param": "n"}}}, "op": "cross_above",
     "right": {"ind": "SMA", "args": {"n": 50}}},
    {"left": {"feature": "CLOSE_VS_VWAP"}, "op": "gt", "right": {"value": 0}}
  ]},
  "exit": {"any": [
    {"left": {"ind": "RSI", "args": {"n": 14}}, "op": "gt", "right": {"value": 70}, "for": 2}
  ]}
}
```

- **Nodes**: `{"all": [...]}`, `{"any": [...]}`, `{"not": node}`, or a condition. At most 3 logical levels on any path, and at most 32 conditions in a rule.
- **Conditions**: `left`, `op`, `right`, and optionally `within: n` (true on at least one of the last n bars, including t) or `for: n` (true on each of the last n bars), with n from 1 to 500.
  - `op` is one of `gt`, `lt`, `ge`, `le`, `cross_above`, `cross_below`.
  - `cross_above` is true at t when `left > right` at t and `left ≤ right` at t − 1; `cross_below` is mirrored.
  - Any NaN makes a condition false.
- **Operands**:
  - `{"ind": NAME, "args": {...}, "out": ..., "src": ..., "tf": ..., "shift": k}`, with `shift` from 0 to 500 bars;
  - `{"value": number}`;
  - `{"series": NAME}`, an external series (§8.5);
  - `{"feature": NAME, "args": {...}}`, on the daily timeline only (§8.2);
  - `{"today": NAME}`, on minute timelines only (§8.3).
  - Any argument or value may be `{"param": "<name>"}`, referring to the rule's `params`.
- **Timeframes**: the rule's `timeframe` is the timeline. On a minute timeline, an operand with `"tf": "1d"` follows §8.4. On the daily timeline, minute information enters only through `feature` operands; other minute timeframes there are invalid.
- **Evaluation**: per symbol, flat → in position when `entry` is true; in position → flat when `exit` is true. `side` is `long` or `short`, and a short rule needs a backtest that allows shorting. The output is DIRECTION, sized by §9.4.
- **Validation** returns every error with its JSON path and a code: UNKNOWN_INDICATOR, BAD_ARGS, BAD_OP, DEPTH, TF_MISMATCH, UNKNOWN_PARAM, UNKNOWN_SERIES, TOO_MANY_CONDITIONS.
- Rules are stored by unique name. A backtest stores the complete rule in its configuration.

### 9.7 Look-ahead check

- Vectorized plugins are checked before each backtest. Compute the targets on the full data, then on the data truncated after each of 5 cut bars, at 50, 60, 70, 80 and 90% of the run. At every bar up to the cut, the truncated targets must equal the full ones (NaN equals NaN; tolerance 1e-9).
- A mismatch sets the flag LOOKAHEAD_SUSPECT with the first bar and symbol that differ. The backtest still runs, and the result shows the warning prominently. **Intentional.**
- An optimization runs the check for its first and its best parameter set.
- Event-driven plugins need no check: `ctx` exposes only rows up to t, as read-only views. Built-ins, indicators and rules are covered by truncation property tests (AC-6, AC-12, AC-14).

## 10. Backtest engine

### 10.1 Run configuration

A run is described by one JSON document, stored with its result:

```json
{
  "name": "MA cross on my US list",
  "market": "US",
  "symbols": ["SPY", "QQQ", "IWM"],
  "list_id": 3,
  "start": "2000-01-03",
  "end": null,
  "timeframe": "1d",
  "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 20, "slow": 50}},
  "sizing": {"mode": "FIXED_FRACTION", "fraction": 0.3333},
  "exits": {"stop_loss_pct": null, "take_profit_pct": null, "trailing_pct": null,
            "atr_stop": null, "max_hold_bars": null, "ibguard": {"tp_pct": 15, "sl_pct": 5}},
  "price_mode": "REALISTIC",
  "fill_timing": "NEXT_OPEN",
  "intrabar_path": "DAILY",
  "allow_short": false,
  "board_lots_only": false,
  "initial_capital": 100000,
  "max_position_pct": 100,
  "costs": {"commission": {"per_share": 0.005, "min": 1.0, "max_pct": 1.0},
            "slippage_bps": 5, "borrow_fee_pct": 0.5, "dividend_withholding_pct": 30},
  "benchmark": "SPY",
  "intraday": null
}
```

- `strategy.source` is `builtin`, `plugin` or `rule`. A rule run carries the complete rule JSON as `strategy.rule`.
- TW commissions are `{"rate_pct": 0.08, "min": 80}`, and TW costs add `short_fee_pct` (0.08).
- `end: null` means the last stored bar. Both dates are inclusive.
- `symbols` is a snapshot of the list at run time.
- **Warm-up**: daily data load from 300 bars before `start`, and minute data from 5 sessions before. Signals use the warm-up; trading and equity start at the first bar on or after `start`.
- Validation names the JSON path of every invalid field (INVALID_FIELD).

### 10.2 Price modes

- **REALISTIC** (default): strategies and indicators use adjusted bars. The engine fills, values and charges costs on raw prices, and applies corporate actions to positions (§10.7).
- **ADJUSTED**: the engine uses adjusted prices for everything and applies no corporate actions: dividends are inside the prices, untaxed. Lot sizes and minimum commissions then act on adjusted prices, which are inexact in early years (§25).

### 10.3 Order of events in a bar

For each bar t, in this order:

1. **Session start** (first bar of a session):
   - corporate actions of the session's day (§10.7);
   - TW forced covers that are due become orders for this bar's open (§10.7);
   - borrow fees accrue on shorts (§10.6).
2. **Open fills** (NEXT_OPEN): pending orders fill at the open of t (§10.5).
3. **Intrabar exits** along the path of t, for every open position, including positions opened at this open (§10.8).
4. **Close**:
   - value positions at the close and record equity;
   - max-hold exits that are due become orders;
   - evaluate the strategy with data up to t, producing orders;
   - under SAME_CLOSE these fill now, at the close, and equity is recorded again after the fills; under NEXT_OPEN they wait for the next bar.
5. **End of the run**: open positions stay open and are valued at the last close. The trade list shows them with exit reason OPEN at that close, without exit costs.

### 10.4 Quantities and lots

- Lots: US 1 share. TW 1 share, or 1,000 shares with `board_lots_only`.
- A target weight becomes `w × equity / price` at the signal bar's close, rounded toward zero to the lot. An order smaller than one lot is skipped.
- Pending orders carry target quantities; a share event before the fill multiplies them by its ratio.

### 10.5 Orders and fills

- An order holds a symbol's target quantity. At the fill, the traded quantity is target minus current.
- **Fill price**: NEXT_OPEN uses the open of the fill bar; SAME_CLOSE uses the close of the signal bar. Buys pay `price × (1 + slippage)`, sells receive `price × (1 − slippage)`.
- **Untradable bars**: an order that cannot fill on a bar (§6.5) stays pending and is retried at each later bar's open (NEXT_OPEN) or close (SAME_CLOSE), until it fills or a newer instruction for the symbol replaces it. When a symbol's data end, its position stays at the last close and the result carries the flag DATA_ENDED.
- **Sequence**: at each fill moment, reducing orders (sells, covers) fill before increasing orders (buys, short sales).
- **Buying power**: when the increasing orders of a fill moment need more than the buying power (§10.7), including slippage and costs, every increasing order is scaled by the same factor and rounded down to the lot.
- **Cash**: a buy pays `quantity × price + costs`; a sell receives `quantity × price − costs`.
- **Trades**: a trade opens when a position leaves 0 and closes when it returns to 0; a reversal closes one trade and opens another. A partial reduction realizes P&L at the average entry price. Trade P&L is net of everything attributable to it: commissions, taxes, short fees, borrow fees, and dividends received or paid while it was open.

### 10.6 Costs

| Item | US | TW |
|---|---|---|
| Commission per fill | `min(max(per_share × q, min), max_pct% × value)` | `max(rate_pct% × value, min)` |
| Slippage | `slippage_bps` on the fill price | same |
| Transaction tax | none | `taxes.toml`, on every sell, short sales included |
| Day-trade tax | none | STOCK_DAYTRADE rate on the part of a sell that closes a same-session round trip in a STOCK |
| Borrow fee | short value at the previous close × `borrow_fee_pct`/100 × calendar days since the previous session / 365 (per-symbol override, §7) | not charged |
| Short-sale fee | none | `short_fee_pct` of the short-sale value, at the sale |
| Dividend withholding | `dividend_withholding_pct` of dividends received by longs | same (default 0) |

- **Day trades**: a sell that closes shares bought in the same session pays the day-trade rate on that quantity. For a short sale covered in the same session, the cover credits back the difference between the normal rate and the day-trade rate on the covered quantity.
- **`taxes.toml`** (committed, owner-editable):

```toml
# Taiwan securities transaction tax on sells. Dates are inclusive; "from" defaults to 1900-01-01, "to" to 9999-12-31.
[[tw]]
category = "STOCK"
rate_pct = 0.3

[[tw]]
category = "STOCK_DAYTRADE"
rate_pct = 0.15
from = "2017-04-28"
to = "2027-12-31"

[[tw]]
category = "ETF"
rate_pct = 0.1

[[tw]]
category = "TDR"
rate_pct = 0.1

[[tw]]
category = "BOND_ETF"
rate_pct = 0.1
to = "2016-12-31"

[[tw]]
category = "BOND_ETF"
rate_pct = 0.0
from = "2017-01-01"
to = "2026-12-31"   # exemption in force; a 10-year extension is pending in the Legislative Yuan

[[tw]]
category = "BOND_ETF"
rate_pct = 0.1
from = "2027-01-01"
```

- Load-time validation: ranges of one category must not overlap, and STOCK, ETF, TDR and BOND_ETF must cover every date; errors name the entries. Outside its range, STOCK_DAYTRADE falls back to STOCK.
- Every default above can be edited per backtest, except the tax table, which lives in the file.

### 10.7 Shorting and corporate actions

**Shorting.**

- Shorts happen only when `allow_short` is true, and only through individual strategy targets. Rotation built-ins are long only. **Intentional.**
- Equity `E = cash + long value − short value`; short-sale proceeds sit in cash.
- Buying power `BP = E − long value − m × |short value|`, with m = 0.5 (US) and 0.9 (TW).
- A buy needs `quantity × price + costs ≤ BP`. A short sale needs `m × quantity × price + costs ≤ BP`. There are no margin calls; when BP is negative, no increasing order fills.
- US shorts pay the borrow fee and the dividends of their ex-dates.
- **TW forced cover.** For each short-sale suspension of a symbol (start s, end e, §5.2):
  - a short still open at the close of the bar before s is covered at the open of the first tradable bar on or after s, with exit reason FORCED_COVER;
  - no short sale fills from s through e; such orders wait.
  - **Intentional**: covering at the suspension's start is earlier than the exchange's last cover day, so it is conservative.

**Corporate actions** in REALISTIC mode, at the open of the ex-date, before any fill:

- SHARE_RATIO r:
  - a quantity q becomes `trunc(q × r)` whole shares;
  - the fractional remainder is settled in cash at `previous raw close / r` (credited to longs, debited to shorts);
  - the price levels of exits (entry, stops, take-profit, trailing peak, ibguard ref and prev) are divided by r;
  - pending target quantities are multiplied by r.
- CASH_DIV D:
  - longs receive `q × D × (1 − withholding)`;
  - shorts pay `|q| × D`;
  - exit levels stay: the ex-dividend drop counts as a price move, as it does at IB and in ibguard. **Intentional.**

### 10.8 Exits and intrabar paths

**Overlays.** Each is optional, they stack, and they apply to every position whatever the strategy. Levels are for longs; shorts are mirrored.

| Overlay | Parameters | Level |
|---|---|---|
| Stop loss (SL) | `stop_loss_pct` | entry × (1 − p) |
| Take profit (TP) | `take_profit_pct` | entry × (1 + p) |
| Trailing stop (TRAIL) | `trailing_pct` | peak × (1 − p); peak is the highest path price since entry, starting at the entry price |
| ATR stop (ATR) | `atr_stop: {n, mult}` | entry − mult × ATR(n) at the signal bar, in the raw scale |
| Max hold (MAX_HOLD) | `max_hold_bars` | Exit at the open of the bar after N bars held (NEXT_OPEN) or at the close of the Nth bar (SAME_CLOSE); the entry bar counts as bar 1 |
| ibguard rule (IBG_TP, IBG_SL) | `ibguard: {tp_pct, sl_pct}` | ibguard's §4.2 rules: ref starts at the entry price; at each path point treated as a tick, the ratchet runs first, then TAKE_PROFIT at `ref × (1 + tp)` and STOP_LOSS at `ref × (1 − sl)` |

- The entry price is the position's average fill price, including slippage. The ibguard rule starts with `ref = prev = entry price`.

**The ibguard rule**, as ibguard's SPEC §4.2 defines it for live quotes. At each tick p (here: each path point), skipping p when it is not finite or ≤ 0:

- Long: (1) if prev is set and `p < prev` and `prev > ref`, then `ref = prev`; (2) if `p ≥ ref × (1 + tp)`: TAKE_PROFIT, else if `p ≤ ref × (1 − sl)`: STOP_LOSS; (3) `prev = p`.
- Short: (1) if prev is set and `p > prev` and `prev < ref`, then `ref = prev`; (2) if `p ≤ ref × (1 − tp)`: TAKE_PROFIT, else if `p ≥ ref × (1 + sl)`: STOP_LOSS; (3) `prev = p`.
- Boundaries are inclusive.

Vectors from ibguard's SPEC §4.4 (tp 0.15, sl 0.05, prev unset at the start). The pure function behind the overlay must reproduce them: the same `ref` after each point and the same exit at the same point. (In a backtest the fill price then follows the path rules below.)

| Id | Side | Start ref | Points in order | ref after each point | Result |
|---|---|---|---|---|---|
| L1 | LONG | 100 | 100, 104, 103 | 100, 100, 104 | no exit |
| L2 | LONG | 100 | 100, 104, 103, 98.7 | 100, 100, 104, 104 | STOP_LOSS at 98.7 |
| L3 | LONG | 100 | 100, 116 | 100, 100 | TAKE_PROFIT at 116 |
| L4 | LONG | 100 | 100, 110, 104 | 100, 100, 110 | STOP_LOSS at 104 |
| L5 | LONG | 100 | 120 | 100 | TAKE_PROFIT at 120 |
| L6 | LONG | 100 | 94 | 100 | STOP_LOSS at 94 |
| L7 | LONG | 100 | 100, 100 | 100, 100 | no exit |
| L8 | LONG | 100 | 97, 98, 97.5 | 100, 100, 100 | no exit |
| L9 | LONG | 100 | 100, 114.9, 114.8, 131 | 100, 100, 114.9, 114.9 | no exit |
| L10 | LONG | 100 | 100, 104, -1, NaN, 0, 103 | 100, 100, 100, 100, 100, 104 | no exit; invalid points are skipped, so 103 is compared with 104 |
| S1 | SHORT | 100 | 100, 96, 97 | 100, 100, 96 | no exit |
| S2 | SHORT | 100 | 100, 96, 97, 100.9 | 100, 100, 96, 96 | STOP_LOSS at 100.9 |
| S3 | SHORT | 100 | 100, 84 | 100, 100 | TAKE_PROFIT at 84 |
| S4 | SHORT | 100 | 100, 90, 95 | 100, 100, 90 | STOP_LOSS at 95 |
| S5 | SHORT | 100 | 106 | 100 | STOP_LOSS at 106 |

`100 * 1.15` evaluates to `114.99999999999999` in floating point; keep test prices away from boundaries that are not exactly representable.

**Paths.**

- **Daily path** (`intrabar_path = DAILY`, or no minute coverage for that day): if close ≥ open, O → L → H → C; otherwise O → H → L → C. The path is continuous between points.
- **Minute path** (`intrabar_path = MINUTE` and the day's coverage is OK): the day's 1-minute bars in order, each contributing O, then L and H in the order of the same rule, then C. The path is continuous inside a minute bar; a minute open that differs from the previous close is a gap.
- **Minute timelines**: the 1-minute bars inside the bar form its path.
- The result counts the position-days that fell back to the daily path for lack of minute data (flag PATH_FALLBACK with the count).

**Triggering.**

- Walk the path points in order. At each point, update trailing peaks and the ibguard ratchet, then check levels.
- A level crossed inside a continuous segment fills at the level. A level already crossed at a gap (the bar's open, or a minute open after a gap) fills at the gap price.
- When several overlays trigger in one segment, the level reached first along the segment wins. On a tie, stops (SL, TRAIL, ATR, IBG_SL) win over profit exits (TP, IBG_TP).
- **Intentional**: this path rule replaces the earlier "assume the stop first" rule; on a down bar the high comes first.
- Exit fills pay slippage, commission and tax like any other fill.
- **Exit reasons**: SL, TP, TRAIL, ATR, MAX_HOLD, IBG_TP, IBG_SL, SIGNAL, FORCED_COVER, CLOSE_OUT (§11.4) and OPEN (§10.3).
- **After an overlay exit**, the symbol's last executed target becomes the strategy's current target. The position re-enters only when the strategy's target changes, or at the next rebalance bar for WEIGHTS outputs.

### 10.9 Benchmark

- The benchmark is BUY_HOLD of the benchmark symbol at 100%, run through the same engine with the run's period, price mode, costs and withholding, and the same initial capital. "Including dividends" therefore means net of the run's withholding.
- When the benchmark lacks data for part of the period, its curve starts at its first bar, and the result carries the flag BENCHMARK_PARTIAL.

### 10.10 Determinism

The engine draws no random numbers. The same data and configuration give bit-identical results.

### 10.11 Result

A run returns:

- the equity curve (per bar on the daily timeline, per session end on minute timelines), cash, gross and net exposure as fractions of equity, and the number of positions;
- the trades;
- the benchmark curve;
- the metrics and tables of §12;
- its flags: SURVIVORSHIP (always), LOOKAHEAD_SUSPECT, IN_SAMPLE_SERIES, SHORTS_IGNORED, PATH_FALLBACK, DATA_ENDED, BENCHMARK_PARTIAL, PDT;
- a data stamp: the latest stored bar and action per symbol of the run.

## 11. Intraday backtests

### 11.1 Timeframes

- Only 1-minute bars are stored. 5, 15, 30 and 60-minute bars are built per session, in buckets aligned to the session's open: bucket k covers minutes `[open + k·N, open + (k + 1)·N)`. A session's last bucket may be shorter.
- A built bar takes the first open, the highest high, the lowest low, the last close and the summed volume. Missing minutes are skipped; an empty bucket gives no bar.

### 11.2 Timeline

- The bars of every session in the run where at least one symbol has coverage OK. At a bar time where a symbol has no bar, its close is carried and it is not tradable.
- A symbol without coverage for a session is not tradable during that session.
- A minute run needs minute data: a symbol without any coverage in the period fails the run with NO_MINUTE_DATA, listing the symbols.

### 11.3 Fills

- NEXT_OPEN fills at the open of the next bar of the same session. An order from a session's last bar fills at the first bar of the next session, unless close-out applies.
- SAME_CLOSE fills at the bar's close.

### 11.4 Close-out

- `intraday.close_out` is `NONE` or `SESSION_END`, with `close_out_minutes` m from 0 to 120.
- With SESSION_END, every position is closed at the close of the last bar that ends at least m minutes before the session's end, with exit reason CLOSE_OUT. No entry fills after that bar in the same session.

### 11.5 Taxes, actions and fees

- Day-trade tax per §10.6.
- Corporate actions apply at the first bar of the ex-date's session; borrow fees accrue per session.
- Daily indicators follow §8.4; today-so-far features follow §8.3.

### 11.6 PDT flag

- US only, informational: behaviour never changes. **Intentional.**
- Count day trades (round trips opened and closed in one session) in every window of 5 consecutive sessions. A window with 4 or more, while the run's equity is below USD 25,000, on sessions before 2026-06-04, sets the flag PDT with the number of such windows.
- 2026-06-04 is the date FINRA's pattern-day-trader rule ended, after the SEC approved its removal on 2026-04-14. Some brokers keep their own limits until 2027-10-20, so the UI explains the flag.

### 11.7 Results

- Metrics use session-end equity (§12); the trade list carries minute timestamps.
- Performance: 10 symbols × 3 years of 1-minute bars in at most 60 s for one run on the owner's PC (§24.3).
- Live portfolios accept daily strategies only (§14.1).

## 12. Metrics and reports

- Equity points are the bars of the daily timeline, or the session ends of minute timelines. `r_t = E_t / E_(t−1) − 1`.
- Periods per year: `P = (number of returns) / (calendar days from the first to the last point / 365.25)`.
- Risk-free rate per period: `rf_t = y_t / 100 / P`, where y_t is the latest DTB3 value on or before the point's day (0 before DTB3's first value). Excess return `e_t = r_t − rf_t`.

| Metric | Definition |
|---|---|
| Total return | `E_T / E_0 − 1` |
| CAGR | `(E_T / E_0)^(365.25 / days) − 1` |
| Annual volatility | `sd(r) × √P`, sample sd |
| Sharpe | `mean(e) / sd(e) × √P` |
| Sortino | `mean(e) / √mean(min(e, 0)²) × √P` |
| Max drawdown | `max_t (1 − E_t / max_(s ≤ t) E_s)` |
| Max drawdown duration | Longest calendar span from a peak until equity regains it, or until the end |
| Calmar | CAGR / max drawdown |
| Win rate | Winning closed trades / closed trades (OPEN excluded) |
| Payoff ratio | Mean winning P&L / \|mean losing P&L\| |
| Profit factor | Sum of wins / \|sum of losses\| |
| Expectancy | Mean P&L per closed trade, in currency and as mean trade return |
| Trades | Closed trades; open ones are reported separately |
| Exposure | Mean gross exposure / equity, and time in market (share of points with a position) |
| Turnover | Sum of \|traded value\| / 2 / mean equity, per year |
| Alpha, beta, correlation | β = cov(e_s, e_b) / var(e_b); α = (mean(e_s) − β·mean(e_b)) × P; correlation of r_s and r_b |
| Annual returns | Per calendar year, compounded |
| Monthly returns | Year × month table, compounded |

- An undefined value (division by zero, too few points) is `null` in JSON and "—" in the UI.
- The benchmark gets the same metrics.
- **CSV exports** (UTF-8 with BOM, so Excel shows Chinese names):
  - `trades.csv`: symbol, side, quantity, entry time, entry price, exit time, exit price, P&L, return %, costs, exit reason;
  - `equity.csv`: time, equity, benchmark, drawdown, gross exposure.
  - Times are ISO 8601 in the market's time zone.

## 13. Optimization

### 13.1 Request

- A base run configuration (§10.1) plus ranges, keyed by JSON path, for example `strategy.params.fast`, `exits.ibguard.tp_pct` or `sizing.risk_pct`. A range is `{min, max, step}` or `{values: [...]}`.
- At most 2,000 combinations per optimization: more is rejected with TOO_MANY_COMBOS. **Intentional.**
- `objective`: SHARPE (default), CAGR, SORTINO, CALMAR or PROFIT_FACTOR. `min_trades`: 20.
- `mode`: GRID (the whole period), OOS or WFO.

### 13.2 Grid

- Every combination runs the engine on the same loaded data, in pool workers. Data reach each worker once, through the pool initializer, as plain arrays.
- Combinations are enumerated lexicographically, in the order the ranges are given.
- **Best**: the highest objective among combinations with at least `min_trades` closed trades. A tie goes to the earlier combination.

### 13.3 Heatmap

Two parameters chosen in the UI; the others are fixed at the best combination's values. Each cell holds the objective, or null below `min_trades`.

### 13.4 Out-of-sample check

- The split bar is the period's first bar plus `floor(0.7 × bars)`. The grid runs on the first 70%; the best combination then runs on the last 30%.
- **Window runs**: data before a window serve as warm-up, and equity starts at the initial capital on the window's first bar.
- The report gives the best combination's in-sample and out-of-sample metrics side by side.

### 13.5 Walk-forward

- Window k trains on `[s_k, s_k + 3 years)` and tests on `[s_k + 3 years, s_k + 4 years)`, with `s_(k+1) = s_k + 1 year` (calendar offsets). A last test window shorter than 3 months is dropped.
- Each window runs the grid on its training period, then its best combination on its test period. A window where no combination reaches `min_trades` is NO_RESULT, and its test period stays in cash.
- **Chained out-of-sample curve**: each test window starts flat with the previous window's final equity, and closes its positions at its last close, with costs.
- The report gives, per window, the chosen parameters, the training objective and the test metrics, then the metrics of the chained curve.

### 13.6 Estimates, progress and cancellation

- Before starting, one combination runs on the full data. `estimate = its time × combinations (× windows for WFO) / workers`.
- When the estimate exceeds 10 minutes, the API answers 409 NEEDS_CONFIRMATION with the estimate, and runs the optimization when the request is repeated with `"confirm_long": true`.
- Progress reports combinations done and total. A cancel ends the job as CANCELLED and keeps the partial results.

### 13.7 Pool and results

- `ProcessPoolExecutor` with `max(1, os.cpu_count() − 1)` workers. Worker functions are top-level and take plain arrays, because Windows spawns workers. The pool is injected; tests use an in-process stand-in.
- Results are stored with their configuration, every combination's metrics, the best combination and the flags.
- **Promote** turns a chosen combination into a saved backtest by running it as an ordinary backtest job.
- Optimizations are deterministic.
- Performance: 2,000 combinations on 50 symbols × 30 years of daily bars within 10 minutes on the owner's PC (§24.3).

## 14. Live portfolios

### 14.1 Definition

- At most one active live portfolio per market and run mode. It is created from a saved backtest on the daily timeline. Minute strategies are backtest-only (TODO.md). **Intentional.**
- It stores: market, run mode, the source backtest's configuration (copied at creation), the allocated capital (default: the backtest's initial capital), the creation time, and the first session it trades.
- Deactivating it keeps its history.

### 14.2 Generating a batch

After every successful data update of its market (§17.2):

1. **Replay**: run the stored configuration, with `end` replaced by the latest stored session. Take the replay's positions after the last close, with each pending order's target quantity in place of the current position where one exists.
2. **Target weights**: `w_s = target quantity × close / replay equity`.
3. **Live equity**: test mode: virtual cash plus virtual holdings at the last close. Trade mode: ledger cash plus the IB-reported quantities of the portfolio's symbols at the last close.
4. **Target quantities**: `q_s = w_s × live equity / close`, rounded toward zero to the live lot. The live lot is 1 share in US. In TW it is the contract's size increment from IB when known (§15.2), otherwise 1,000. **Intentional**: backtests still simulate odd lots.
5. **Lines**: Δ = target − current (test: virtual holdings; trade: IB positions). Lines smaller than one lot are dropped.
6. **Skips**, kept in the batch with a reason and never sent:
   - SKIPPED_TW_SHORT: a TW line that opens or enlarges a short. US short lines are sent, and IB rejects them when no shares can be borrowed. **Intentional.**
   - SKIPPED_NO_CONTRACT: no unique IB contract (trade mode).
   - SKIPPED_NO_PRICE: no last close.
7. **Limits** in trade mode (§15.3): BLOCKED_LIMIT.

Each line holds action (BUY or SELL), quantity, order kind (US: MOO or LIMIT per the config; TW: LIMIT), limit price, reference price (the last close) and its reason.

### 14.3 Timing

- A batch is for the session after the latest stored session. US batches follow the US update (Tuesday–Saturday 08:30 Asia/Taipei) for that evening's session; TW batches follow the TW update (Monday–Friday 18:00) for the next morning.
- A later update, or a regenerate request, replaces a PROPOSED batch. A batch that is CONFIRMED or later is never changed.

### 14.4 Batch states

- PROPOSED → CONFIRMED (by the owner) → SENT (trade mode) → DONE. PROPOSED → EXPIRED at the confirmation cutoff: 09:20 America/New_York for US, 08:50 Asia/Taipei for TW. Superseded batches are REPLACED.
- On confirmation the owner may uncheck lines; they become SKIPPED_OWNER. Confirmation needs a PROPOSED batch before its cutoff.
- An optional email says that a batch is ready (§17.6).

### 14.5 Virtual fills (test mode)

- Confirmed lines become VIRTUAL_PENDING. When the data of the batch's session arrive, each fills by the backtest rules on that session's daily bar:
  - MOO: at the open;
  - LIMIT buy: at the open when open ≤ limit, else at the limit when low ≤ limit, else EXPIRED; LIMIT sell mirrored;
  - with the slippage and costs of the stored configuration;
  - an untradable or locked bar expires the line. Day orders never carry over.
- Virtual holdings and cash update; corporate actions apply to them by the REALISTIC rules (§10.7).

### 14.6 Ledger and curves

- One ledger row per portfolio per session close: cash, equity and holdings.
  - Test mode: the virtual book.
  - Trade mode: ledger cash (allocated capital, plus and minus fills and costs, plus dividends net of withholding) and IB quantities. When IB quantities differ from the quantities the ledger expects, an event HOLDINGS_MISMATCH records it; lines always use IB quantities.
- The UI charts the portfolio's equity against the benchmark (bought at the first session with the same capital) and against the replayed backtest, rescaled to the portfolio's first session.

## 15. Orders through IB (trade mode)

### 15.1 Guarantees

- `mode = "trade"` in `config.toml` selects trade mode at startup. Nothing at run time changes the run mode. `config.example.toml` sets `"test"`.
- Test mode never constructs the order adapter of `ib_orders.py`, connects with `readonly=True`, and requests no orders.
- `placeOrder` and `cancelOrder` appear in `ib_orders.py` and nowhere else, and btlab never calls `reqGlobalCancel`; a test enforces both.
- `trading.py` depends only on an `OrderPort` protocol (place, cancel own, open orders, completed orders, executions, order events) and on domain dataclasses. No ib_async type crosses that boundary; tests use a fake.
- Trade mode refuses to start while the limits of §15.3 are missing or not positive; the error names the keys.
- The UI shows the run mode and, in trade mode, the account kind at all times; a live account adds a red banner.

### 15.2 Contracts and prices

- Contracts resolve as in §5.4 and are cached with conId, exchange, currency, minimum size, size increment and market-rule ids. Verify the field names against the installed `ib_async` 2.1.0 source.
- TW quantities round down to the size increment (1,000 when IB gives none). A quantity below the minimum size is SKIPPED_MIN_SIZE.
- Limit prices round to the increment of the contract's market rule (`reqMarketRuleAsync`): buys down, sells up. An unknown rule gives SKIPPED_NO_PRICE_RULE.

### 15.3 Limits

The `[limits]` table of `config.toml` is required in trade mode and has no defaults: the owner chooses the numbers. **Intentional.**

| Key | Meaning |
|---|---|
| `us_max_orders_per_day`, `tw_max_orders_per_day` | Lines sent per market per session |
| `max_order_pct_of_equity` | A line's value may not exceed this percentage of the live equity |
| `us_max_order_value` (USD), `tw_max_order_value` (TWD) | A line's value may not exceed this amount |

- Limits are checked when the batch is generated and again just before sending. A line above a value limit is BLOCKED_LIMIT. When the lines exceed the count limit, the largest ones up to the limit stay and the rest are BLOCKED_LIMIT.
- Limits are changed only in the config file; the UI shows them read-only.

### 15.4 Orders

- US MOO: `MarketOrder(action, quantity)` with `tif = "OPG"`.
- US LIMIT: `LimitOrder(action, quantity, price)` with `tif = "DAY"` and price = reference × (1 ± `us_limit_offset_pct`/100), plus for buys and minus for sells.
- TW: `LimitOrder` with `tif = "DAY"` and price = reference × (1 ± `tw_limit_offset_pct`/100), clipped inside the day's price limits.
- Every order: `outsideRth = False`, `account` set, and `orderRef = "btlab:<batch>:<line>:<UTC epoch ms>"`, unique per send and stored with the line before sending.

### 15.5 Sending

Sending runs as a LIVE job when the owner confirms a batch.

- **Preconditions**: socket and upstream connected and the startup sync complete; the account is the configured one (or the only one); the cutoff has not passed; the limits pass again; and IB's positions still match those the batch was built on. Otherwise the job refuses with a reason; changed positions give 409 BATCH_STALE, and the owner regenerates.
- **Order**: reducing lines (sells, covers) first, then increasing lines. Every reducing line on a position that ibguard protects goes through §16.2 first.
- **One send function** refuses an order when: the quantity is not above 0; a reducing line would trade more than the position; a btlab order already works on the contract; or the run mode is not trade.
- **Line states**: SENT → WORKING → FILLED, PARTIAL (ended partly filled), CANCELLED, REJECTED or EXPIRED. Fills, with quantity, average price and commission, update the ledger.

### 15.6 Monitoring

- The IB link stays connected while a btlab order works or an exclusion is active, and until 15 minutes after the session's end. It reconnects after 10 s, doubling up to 60 s.
- 15 minutes after the session's end, lines that have not ended are checked against IB's open and completed orders. Day orders that IB reports cancelled at the end of the day become EXPIRED.

### 15.7 Recovery

- At every connect, btlab requests this client's open orders and the completed orders. An order whose `orderRef` starts with `btlab:` is matched to its line by the exact `orderRef`, and its state and fills are reconciled.
- A `btlab:` order that matches no line is recorded as an event and left alone.

### 15.8 Other orders

- Orders without the `btlab:` prefix, or placed by another client id, are never modified or cancelled. **Intentional.**
- The owner can cancel btlab's own working orders of a batch from the UI before the session ends.

### 15.9 Errors

- A rejection because the API is read-only (IB error 321) records READ_ONLY_API and stops the batch.
- Other rejections are per line.
- An unexpected exception in the trading path stops the batch and records INTERNAL_ERROR.
- Nothing is retried automatically; before the cutoff, the owner can regenerate.

## 16. ibguard coordination

### 16.1 Client

- `btlab/ibguard_client.py` uses ibguard's JSON API (ibguard SPEC §11). Base URL (default `http://127.0.0.1:8080`) and token from the config; an empty URL disables coordination, and the UI then warns that sells are not coordinated.
- Calls: `POST /api/login` with `{"token": ...}` (the session cookie lives in memory only); `GET /api/state`; `POST /api/thresholds` with `{"STK": {"tp_pct": number, "sl_pct": number}}`; `POST /api/positions/REAL/{con_id}/exclude` with `{"excluded": bool}`. The request rules of §5.7 apply.

### 16.2 Selling a position that ibguard protects

For each reducing line in trade mode:

1. Read ibguard's state. If ibguard is configured but unreachable, the line is SKIPPED_IBGUARD_UNREACHABLE and is not sent; increasing lines still go. **Intentional**: without ibguard's state, a btlab sell could race an ibguard exit and open a reverse position.
2. Find ibguard's REAL position with the line's conId. When there is none, send normally.
3. When its status is EXITING, EXIT_PENDING or POSITION_UNCONFIRMED, or its `pending_exit` is set: SKIPPED_IBGUARD_EXITING.
4. When the owner has already excluded it (EXCLUDED): send, and never change that exclusion.
5. Otherwise exclude it, read the state again, and require EXCLUDED (else SKIPPED_IBGUARD_EXCLUDE_FAILED). Store an exclusion row (ACTIVE) linked to the line, then send.
6. When the line's order ends: if IB still reports a position on the contract, include it again (row RESTORED) and notify PROTECTION_RESTORED with the filled quantity. If the position is closed, the row becomes CLOSED; ibguard drops its own row.
7. **Safety sweep**: 15 minutes after the session's end, at every btlab start and at every IB connect, each ACTIVE row whose order no longer works is resolved as in step 6. A failing include is retried every minute and raises one event and one email.

### 16.3 Writing thresholds

- Source: a saved backtest whose exits use the ibguard rule. Its `tp_pct` and `sl_pct` are the new STK thresholds. To use an optimization result, promote the combination to a backtest first.
- **Preview**: ibguard's account, account kind and paused state; the current and the new STK thresholds; and every STK position, US and Taiwan, that would exit at once under the new thresholds, with symbol, side, quantity, ref, price and reason. For a long, `price ≥ ref × (1 + tp)` gives TAKE_PROFIT and `price ≤ ref × (1 − sl)` gives STOP_LOSS; shorts are mirrored. Positions without a price are listed as unknown.
- The preview carries a token: the SHA-256 of the canonical JSON of the account, the new thresholds and the affected positions.
- **Write**: btlab previews again. A different token gives 409 PREVIEW_CHANGED; otherwise it posts the STK thresholds only and records THRESHOLDS_WRITTEN. Other security types are untouched. Values must satisfy ibguard's ranges: `tp_pct` in (0, 1000], `sl_pct` in (0, 100).
- ibguard keeps one STK pair for both markets; separate thresholds per market need an ibguard change (TODO.md). **Intentional.**
- Writing thresholds works in both btlab run modes: it changes ibguard, not btlab orders.

## 17. Jobs and scheduling

### 17.1 Lanes

| Lane | Job kinds | Cancel |
|---|---|---|
| DATA | UPDATE_US, UPDATE_TW, SPYLEAD_SNAPSHOT | yes |
| MINUTE | MINUTE_BACKFILL | yes |
| COMPUTE | BACKTEST, OPTIMIZE | yes |
| LIVE | LIVE_GENERATE, LIVE_VIRTUAL_FILL, LIVE_SEND, LIVE_CANCEL, LIVE_SWEEP | no |

- One job per lane at a time. A request for a busy lane gets 409 JOB_RUNNING. The MINUTE lane pauses while a DATA job runs and resumes afterwards.
- Progress, shown in the state document: kind, phase, done, total, current item, start time, and a message, for example "waiting for Tiingo's hourly limit until 14:05".
- A cancel ends the job as CANCELLED. Data stored so far stay; an optimization keeps its partial results.
- Shutdown interrupts running jobs as INTERRUPTED, with the same rules.
- Every job's end is stored (`job_runs`, §18) and recorded as an event.

### 17.2 Data update of a market

- Phases: CALENDAR (TW), INFO (TW, at most once a day), BARS, ACTIONS (per symbol, §5.1, §5.2), RATES (DTB3, §5.3), SNAPSHOT (spylead, when configured, §5.6).
- One failing symbol stores its error on its row (`last_fetch_error`) and the job continues.
- Outcomes: UP_TO_DATE (nothing new stored), UPDATED (with counts), PARTIAL (some symbols failed), FAILED (for example MISSING_KEY), MAINTENANCE (FinMind's Sunday window).
- **Follow-ups**, after UPDATED or PARTIAL:
  1. LIVE_VIRTUAL_FILL for test-mode lines of the session just stored (§14.5);
  2. MINUTE_BACKFILL for the new sessions of minute symbols, when IB is configured (§17.3);
  3. LIVE_GENERATE for the market's active live portfolio (§14.2). It needs a bar, or a no-trade day, on the latest session for every symbol of the portfolio. Otherwise no batch is made, an event LIVE_DATA_INCOMPLETE is recorded, and the next update tries again.

### 17.3 Minute backfill

- The queue holds every (symbol, session) for minute symbols (§5.5) from `backfill_years` back to the latest stored daily bar, without coverage OK, EMPTY, NO_PERMISSION or MISMATCH. FAILED sessions are retried.
- Newest sessions first; pacing per §5.4; progress shows symbol, session, done, total and an estimated end.
- The job ends on cancel, shutdown, or when IB cannot be reached (IB_UNREACHABLE); the next update or a manual start resumes it from the coverage table.
- With `ib.host` empty, minute features are off and the UI says so.

### 17.4 Schedules

- TW update: Monday–Friday 18:00 Asia/Taipei (FinMind publishes daily prices around 17:30). US update: Tuesday–Saturday 08:30 Asia/Taipei. Both are set in `[schedule]`; `enabled` turns them off.
- **Catch-up**: every minute the scheduler compares each schedule's latest due time with its latest run. When a due time has passed and no run started after it, the update runs now. A PC that was off runs one missed update at start, not one per missed day. **Intentional.**
- A scheduled update that ends FAILED retries after 30 minutes, at most 3 times, then emails UPDATE_FAILED. MAINTENANCE retries at 07:05.
- A due update waits while the DATA lane is busy.
- Confirmation cutoffs (§14.4) and the sweeps 15 minutes after each session's end (§15.6, §16.2) are scheduler events too.
- `scheduler.py` is pure: it takes the clock time, the schedule and the last runs, and returns what is due.

### 17.5 Backtest and optimization jobs

- BACKTEST phases: LOAD (store to arrays), CHECK (look-ahead check for vectorized plugins), SIMULATE (in a pool worker), METRICS, SAVE.
- OPTIMIZE phases: LOAD, ESTIMATE, GRID, OOS or WFO, SAVE.
- A result is saved in one transaction.

### 17.6 Notifications

- `btlab/notifier.py`: optional SMTP, as in ibguard. `smtplib` runs through `asyncio.to_thread`; port 465 uses implicit TLS, any other port STARTTLS. An empty `smtp_host` disables email with a startup warning. A failed send is logged and never affects a job.
- Subjects start with `[btlab]`, and with `[btlab][TRADE]` in trade mode. Language from `email.language` (`zh-TW` by default, or `en`).

| Email | When |
|---|---|
| BATCH_READY | A batch is proposed; with its session and cutoff |
| BATCH_DONE | All lines of a sent batch have ended; with fills |
| PROTECTION_RESTORED | Once per position (§16.2) |
| IBGUARD_RESTORE_FAILED | Once per position (§16.2) |
| UPDATE_FAILED | A scheduled update failed 3 times |
| READ_ONLY_API | Every read-only rejection (§15.9) |
| INTERNAL_ERROR | Once per run of the app |

## 18. Persistence

- Two SQLite files next to the config file: `data/btlab.db` and `data/minute.db`. Minute bars live apart because they are large and written in the background.
- Each file: `PRAGMA journal_mode = WAL` with `PRAGMA synchronous = NORMAL`, falling back to `DELETE` with a warning when WAL cannot be enabled (some external drives); busy timeout 1000 ms; `PRAGMA user_version = 1`.
- Each file sits behind one synchronous store class. Writes are transactions; failures surface as one store error type.
- Arrays are float64 little-endian BLOBs.

Starting schema. Deviate only with a reason stated in the plan.

```sql
-- data/btlab.db
CREATE TABLE symbols (
  market           TEXT NOT NULL,          -- US | TW
  symbol           TEXT NOT NULL,
  name             TEXT,
  category         TEXT NOT NULL,          -- STOCK | ETF | BOND_ETF | TDR
  category_source  TEXT NOT NULL,          -- RULE | OWNER
  exchange         TEXT,                   -- TW: TWSE | TPEX
  minute_enabled   INTEGER NOT NULL DEFAULT 0,
  borrow_fee_pct   REAL,                   -- US override; NULL = the run's default
  last_fetch_at    TEXT,                   -- UTC ISO 8601
  last_fetch_error TEXT,                   -- code and English text; never a URL, key or token
  added_at         TEXT NOT NULL,
  PRIMARY KEY (market, symbol)
);
CREATE TABLE lists (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  market     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE list_members (
  list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
  symbol   TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (list_id, symbol)
);
CREATE TABLE daily_bars (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL NOT NULL,
  no_trade INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (market, symbol, day)
) WITHOUT ROWID;
CREATE TABLE actions (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  kind   TEXT NOT NULL,                    -- CASH_DIV | SHARE_RATIO
  detail TEXT NOT NULL,                    -- SPLIT | REVERSE_SPLIT | STOCK_DIV | PAR_CHANGE | CAPITAL_REDUCTION | DIV_RESULT
  cash   REAL NOT NULL DEFAULT 0,
  ratio  REAL NOT NULL DEFAULT 1,
  ref_before REAL, ref_after REAL,         -- TW dividend results
  PRIMARY KEY (market, symbol, day, kind)
) WITHOUT ROWID;
CREATE TABLE trading_days (market TEXT NOT NULL, day INTEGER NOT NULL, PRIMARY KEY (market, day)) WITHOUT ROWID;
CREATE TABLE short_suspensions (               -- TW
  symbol TEXT NOT NULL, start_day INTEGER NOT NULL, end_day INTEGER NOT NULL, reason TEXT,
  PRIMARY KEY (symbol, start_day)
) WITHOUT ROWID;
CREATE TABLE rf_rates (day INTEGER PRIMARY KEY, rate_pct REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE external_series (                 -- latest spylead snapshot
  name TEXT PRIMARY KEY, first_day INTEGER NOT NULL, values_blob BLOB NOT NULL,
  in_sample INTEGER NOT NULL, snapshot_at TEXT NOT NULL
);
CREATE TABLE contracts (
  market TEXT NOT NULL, symbol TEXT NOT NULL, con_id INTEGER NOT NULL,
  exchange TEXT, currency TEXT NOT NULL, min_size REAL, size_increment REAL,
  market_rule_ids TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (market, symbol)
);
CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE backtests (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
  market TEXT NOT NULL, timeframe TEXT NOT NULL,
  config TEXT NOT NULL,                    -- JSON, §10.1
  data_stamp TEXT NOT NULL,                -- JSON
  metrics TEXT NOT NULL, tables TEXT NOT NULL, flags TEXT NOT NULL,   -- JSON
  keep_offline INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT                           -- last GET of the detail; drives the offline set (§20.5)
);
CREATE TABLE backtest_series (
  backtest_id INTEGER NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                      -- TIME | EQUITY | BENCHMARK | CASH | GROSS | NET | POSITIONS
  values_blob BLOB NOT NULL,
  PRIMARY KEY (backtest_id, kind)
);
CREATE TABLE trades (
  backtest_id INTEGER NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
  entry_time INTEGER NOT NULL, entry_price REAL NOT NULL,
  exit_time INTEGER, exit_price REAL, pnl REAL, return_pct REAL, costs REAL NOT NULL,
  exit_reason TEXT NOT NULL,
  PRIMARY KEY (backtest_id, seq)
) WITHOUT ROWID;
CREATE TABLE optimizations (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL, market TEXT NOT NULL,
  mode TEXT NOT NULL,                      -- GRID | OOS | WFO
  status TEXT NOT NULL,                    -- DONE | CANCELLED | INTERRUPTED | FAILED
  config TEXT NOT NULL, results TEXT NOT NULL, best TEXT, flags TEXT NOT NULL
);
CREATE TABLE live_portfolios (
  id INTEGER PRIMARY KEY, market TEXT NOT NULL, mode TEXT NOT NULL,
  backtest_id INTEGER, config TEXT NOT NULL, capital REAL NOT NULL,
  created_at TEXT NOT NULL, first_day INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX one_active_portfolio ON live_portfolios (market, mode) WHERE active = 1;
CREATE TABLE live_batches (
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES live_portfolios(id),
  session_day INTEGER NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL,                    -- PROPOSED | CONFIRMED | SENT | DONE | EXPIRED | REPLACED
  confirmed_at TEXT, positions_seen TEXT NOT NULL   -- JSON: the holdings the batch was built on
);
CREATE TABLE live_orders (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES live_batches(id),
  line INTEGER NOT NULL, symbol TEXT NOT NULL, con_id INTEGER,
  action TEXT NOT NULL, quantity REAL NOT NULL, kind TEXT NOT NULL,   -- MOO | LIMIT
  limit_price REAL, ref_price REAL NOT NULL,
  status TEXT NOT NULL, reason TEXT,
  order_ref TEXT UNIQUE, ib_order_id INTEGER,
  filled_qty REAL NOT NULL DEFAULT 0, avg_price REAL, commission REAL, updated_at TEXT NOT NULL
);
CREATE TABLE live_holdings (                   -- test mode: the virtual book
  portfolio_id INTEGER NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
  PRIMARY KEY (portfolio_id, symbol)
);
CREATE TABLE live_ledger (
  portfolio_id INTEGER NOT NULL, day INTEGER NOT NULL,
  cash REAL NOT NULL, equity REAL NOT NULL, holdings TEXT NOT NULL,  -- JSON
  PRIMARY KEY (portfolio_id, day)
) WITHOUT ROWID;
CREATE TABLE ibguard_exclusions (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES live_orders(id),
  con_id INTEGER NOT NULL, symbol TEXT NOT NULL,
  state TEXT NOT NULL,                     -- ACTIVE | RESTORED | CLOSED
  excluded_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE job_runs (
  id INTEGER PRIMARY KEY, lane TEXT NOT NULL, kind TEXT NOT NULL,
  scheduled_for TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT, message TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- data_start/<market>, request logs, ...
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  level TEXT NOT NULL,                     -- INFO | WARNING | ERROR
  kind TEXT NOT NULL, market TEXT, symbol TEXT, message TEXT NOT NULL
);

-- data/minute.db
CREATE TABLE minute_bars (
  market TEXT NOT NULL, symbol TEXT NOT NULL, t INTEGER NOT NULL,   -- UTC epoch second of the bar's start
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
  PRIMARY KEY (market, symbol, t)
) WITHOUT ROWID;
CREATE TABLE minute_coverage (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  status TEXT NOT NULL,                    -- OK | EMPTY | FAILED | NO_PERMISSION | MISMATCH
  factor REAL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (market, symbol, day)
) WITHOUT ROWID;
```

- Event kinds:
  - data: UPDATE_STARTED, UPDATE_FINISHED, UP_TO_DATE, SYMBOL_FAILED, BAD_ACTION, DIVIDEND_SPLIT_UNKNOWN, TIINGO_SYMBOL_BUDGET, SPYLEAD_UNREACHABLE, MINUTE_MISMATCH, MINUTE_NO_PERMISSION;
  - runs: BACKTEST_SAVED, OPTIMIZATION_SAVED, JOB_FAILED, JOB_CANCELLED, JOB_INTERRUPTED;
  - live: BATCH_PROPOSED, BATCH_CONFIRMED, BATCH_EXPIRED, ORDER_SENT, ORDER_ENDED, HOLDINGS_MISMATCH, LIVE_DATA_INCOMPLETE, UNKNOWN_BTLAB_ORDER, READ_ONLY_API;
  - ibguard: EXCLUDED, PROTECTION_RESTORED, IBGUARD_RESTORE_FAILED, THRESHOLDS_WRITTEN;
  - other: SETTING_CHANGED, INTERNAL_ERROR.

## 19. Web API

| Method and path | Body | Result |
|---|---|---|
| `POST /api/login` | `{"token"}` | 204 and the session cookie; 401 LOGIN_FAILED after a 1 s delay |
| `POST /api/logout` | `{}` | 204, cookie cleared |
| `GET /api/state` | | 200, the state document |
| `GET /api/lists` | | 200, lists with their symbols |
| `POST /api/lists` | `{"name", "market"}` | 201 `{"id"}`; 409 ALREADY_EXISTS |
| `PUT /api/lists/{id}` | `{"name"?, "symbols"?}` | 204; 400 INVALID_FIELD for a bad symbol |
| `DELETE /api/lists/{id}` | | 204 |
| `GET /api/symbols?market=` | | 200: per symbol: name, category, exchange, data range, last bar, last error, minute flag and coverage |
| `PATCH /api/symbols/{market}/{symbol}` | `{"category"?, "minute_enabled"?, "borrow_fee_pct"?}` | 204 |
| `DELETE /api/symbols/{market}/{symbol}` | | 204 (purge, §7); 409 IN_USE while in a list or live portfolio |
| `POST /api/update` | `{"market"}` | 202; 409 JOB_RUNNING |
| `POST /api/minute/backfill` | `{"market"?}` | 202; 409 JOB_RUNNING or IB_NOT_CONFIGURED |
| `POST /api/jobs/{lane}/cancel` | `{}` | 204; 409 NOT_CANCELLABLE |
| `GET /api/candles/{market}/{symbol}?tf=&from=&to=&adjusted=` | | 200 candle payload |
| `GET /api/strategies` | | 200: built-ins and plugins with schemas, timeframes, styles and load errors |
| `POST /api/strategies/reload` | `{}` | 200, the same list |
| `GET /api/indicators` | | 200, the registry of §8.6 |
| `GET /api/rules`, `POST /api/rules`, `PUT /api/rules/{id}`, `DELETE /api/rules/{id}` | `{"name", "rule"}` | Rule storage; 400 RULE_INVALID with the errors of §9.6 |
| `POST /api/rules/validate` | `{"rule"}` | 200 `{"ok", "errors"}` |
| `POST /api/backtests` | `{"config"}` | 202 `{"job"}`; 400 INVALID_FIELD; 409 JOB_RUNNING |
| `GET /api/backtests?market=&limit=` | | 200 summaries: id, name, created, market, strategy, period, key metrics, flags, keep_offline |
| `GET /api/backtests/{id}` | | 200: configuration, metrics, tables, flags; updates `opened_at` |
| `GET /api/offline` | | 200 `{"ids": [...]}`: the 20 most recently opened results plus every keep-offline result (§20.5) |
| `GET /api/backtests/{id}/series` | | 200 series payload |
| `GET /api/backtests/{id}/trades?offset=&limit=` | | 200 trades |
| `GET /api/backtests/{id}/candles/{symbol}?from=&to=` | | 200 candle payload with trade markers |
| `GET /api/backtests/{id}/export/trades.csv`, `.../equity.csv` | | 200 CSV (§12) |
| `PATCH /api/backtests/{id}` | `{"name"?, "keep_offline"?}` | 204 |
| `DELETE /api/backtests/{id}` | | 204; 409 IN_USE when a live portfolio uses it |
| `GET /api/compare?ids=` | | 200: 2 to 4 runs, curves rescaled to 1 and aligned, metrics side by side |
| `POST /api/optimizations` | `{"config", "ranges", "objective", "min_trades", "mode", "confirm_long"?}` | 202; 400 TOO_MANY_COMBOS; 409 NEEDS_CONFIRMATION `{"estimate_seconds"}` or JOB_RUNNING |
| `GET /api/optimizations`, `GET /api/optimizations/{id}` | | 200 |
| `GET /api/optimizations/{id}/heatmap?x=&y=` | | 200 matrix |
| `POST /api/optimizations/{id}/promote` | `{"combo"}` | 202 (a BACKTEST job) |
| `DELETE /api/optimizations/{id}` | | 204 |
| `GET /api/live` | | 200: portfolios of the current run mode, holdings, latest batch |
| `POST /api/live` | `{"backtest_id", "capital"?}` | 201; 409 ALREADY_EXISTS; 400 NOT_DAILY |
| `DELETE /api/live/{id}` | | 204 (deactivate) |
| `GET /api/live/{id}/curve` | | 200 series payload: portfolio, benchmark, replay |
| `GET /api/live/{id}/batches?limit=` | | 200 batches with lines |
| `POST /api/live/{id}/regenerate` | `{}` | 202; 409 BATCH_LOCKED |
| `POST /api/live/batches/{id}/confirm` | `{"lines": [ids]}` | 202; 409 BATCH_EXPIRED, BATCH_STALE, LIMITS_NOT_SET, NOT_CONNECTED |
| `POST /api/live/batches/{id}/cancel` | `{}` | 202 (cancel btlab's working orders) |
| `GET /api/ibguard/preview?backtest_id=` | | 200 preview with token; 400 NO_IBGUARD_EXIT; 409 IBGUARD_UNREACHABLE |
| `POST /api/ibguard/thresholds` | `{"backtest_id", "preview_token"}` | 204; 409 PREVIEW_CHANGED or IBGUARD_UNREACHABLE |
| `GET /api/spylead` | | 200: snapshot time and series |
| `POST /api/spylead/snapshot` | `{}` | 202 (DATA lane) |
| `GET /` and static assets | | The UI. Static files need no session. |

Rules:

- **Session cookie**: its value is the hex HMAC-SHA256 of `btlab-session`, keyed with the configured token. `HttpOnly`, `SameSite=Strict`, `Max-Age` 30 days; `Secure` is added when the request carries `X-Forwarded-Proto: https`. Tokens and cookies are compared in constant time.
- **Authentication**: every `/api/*` route except login requires the cookie; otherwise 401 UNAUTHORIZED.
- **Request bodies**: POST, PUT and PATCH bodies must be `application/json` (415 UNSUPPORTED_MEDIA_TYPE); invalid JSON gets 400 INVALID_JSON; numbers must be finite JSON numbers, and `NaN`, `Infinity` and booleans in number fields get 400 INVALID_FIELD.
- **Errors**: `{"error": "<code>", "message": "<English text>", "path"?: "<JSON path>"}`. An unknown id gives 404 NOT_FOUND. The UI localizes by code.
- **Listening**: 127.0.0.1 only. There is no host setting; the port comes from the config.
- **Headers**: every response carries the headers of §21.
- **Payloads**:
  - times are day integers on the daily timeline and UTC epoch seconds on minute timelines; every payload says which (`"time_unit": "day"` or `"second"`);
  - values are rounded to 6 significant digits; NaN and infinities become null;
  - a series payload is `{"time_unit", "t": [...], "series": {"<name>": [...]}}`;
  - a candle payload is `{"time_unit", "t", "open", "high", "low", "close", "volume", "markers": [{"t", "side", "price", "quantity", "reason"}]}`, at most 5,000 bars per request; the UI asks for windows with `from` and `to`.
- **State document**:

```json
{
  "app": {
    "server_time": "2026-10-29T06:03:11Z", "mode": "test",
    "keys": {"tiingo": true, "finmind": true, "fred": true},
    "services": {
      "ib": {"configured": true, "connected": false, "account_kind": null, "permissions": {"US": "OK", "TW": "NO_PERMISSION"}},
      "ibguard": {"configured": true, "reachable": true, "paused": false, "account_kind": "paper"},
      "spylead": {"configured": true, "snapshot_at": "2026-10-29T00:31:02Z", "series": 6}
    },
    "limits": null,
    "schedule": {"enabled": true, "next": {"US": "2026-10-30T00:30:00Z", "TW": "2026-10-29T10:00:00Z"}}
  },
  "markets": {
    "US": {"data_start": "1995-10-29", "last_day": "2026-10-28", "symbols": 48,
           "last_update": {"outcome": "UPDATED", "finished_at": "2026-10-29T00:41:10Z", "message": "..."}},
    "TW": {"data_start": "1995-10-29", "last_day": "2026-10-28", "symbols": 50, "last_update": null}
  },
  "jobs": {"DATA": {"kind": "UPDATE_US", "phase": "BARS", "done": 12, "total": 48, "item": "AAPL",
                    "started_at": "2026-10-29T00:30:04Z", "message": null},
           "MINUTE": null, "COMPUTE": null, "LIVE": null},
  "live": {"US": {"portfolio_id": 1, "batch": {"id": 7, "status": "PROPOSED", "session": "2026-10-29",
                                                "cutoff": "2026-10-29T13:20:00Z", "lines": 5}},
           "TW": null},
  "events": [{"id": 412, "ts": "...", "level": "INFO", "kind": "UPDATE_FINISHED", "market": "US", "symbol": null, "message": "..."}]
}
```

  - `keys` and `services` say only whether something is set or reachable, never a key, a token or a URL.
  - `limits` is null in test mode and the read-only limits in trade mode.
  - `events` holds the latest 50, newest first.

## 20. Web UI and PWA

### 20.1 Technology

- Static files, no framework, no build step, no requests to other hosts.
- Charts use Apache ECharts 6.1.0, vendored at `btlab/static/vendor/echarts/` with its LICENSE and NOTICE. The file's SHA-256 is `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0`. Do not replace or upgrade it.
- Never insert server data as HTML. Use `textContent` and DOM APIs: no `innerHTML`, no inline script, no inline style, no inline event handlers. The CSP of §21 forbids them.
- ECharts uses the canvas renderer and `renderMode: 'richText'` tooltips.

### 20.2 Screens

One page with seven views (Home, Data, Backtest, Results, Optimize, Live, Settings) in a bottom tab bar on phones and a side bar on wide screens. The rule builder opens from the Backtest view.

1. **Home**
   - the run-mode badge: TEST, TRADE·paper, or TRADE·live with a red banner;
   - per market: last bar, last update with outcome and time, next scheduled update;
   - job progress per lane, with cancel where allowed;
   - IB, ibguard and spylead status;
   - batches waiting for confirmation, with a countdown to the cutoff;
   - the latest events.
2. **Data**
   - lists: create, rename, delete; add, remove and reorder symbols;
   - the symbols table: name, category (with override), exchange, data range, last bar, last error, the minute toggle with coverage, purge;
   - [Update US], [Update TW], minute backfill start and stop with progress, the spylead snapshot with [Snapshot now].
3. **Backtest**: the run form of §10.1, grouped:
   - universe: market, list, symbols, period;
   - strategy: built-in, plugin (with load errors) or rule, and the parameter form generated from its schema;
   - sizing; exits (one toggle and its fields per overlay); execution: price mode, fill timing, intrabar path, shorting, board lots, close-out for minute timelines;
   - costs, prefilled per market and editable; capital and benchmark;
   - validation errors appear at their fields, matched by JSON path.
4. **Rule builder**, opened from the Backtest view:
   - a tree of groups (ALL, ANY, NOT) and conditions, at most 3 levels deep;
   - a condition row: left operand (indicator, feature, today-so-far, external series or value, with arguments, timeframe and shift), operator, right operand, optional `within` or `for`;
   - the rule's parameters; live validation through `POST /api/rules/validate`; save and load by name; a read-only JSON view.
5. **Results**
   - the list, filtered by market and name, with key metrics and flags;
   - a result's page:
     - flags: SURVIVORSHIP as a quiet note; LOOKAHEAD_SUSPECT, IN_SAMPLE_SERIES, PDT, PATH_FALLBACK, DATA_ENDED, SHORTS_IGNORED and BENCHMARK_PARTIAL as prominent banners;
     - metric cards, strategy next to benchmark;
     - charts: equity against benchmark with a log-axis toggle, drawdown, a monthly-return heatmap, annual returns, exposure, and candles with trade markers for a chosen symbol;
     - the paged trades table;
     - actions: rename, keep offline, export CSV, delete, compare, use for a live portfolio, write to ibguard (when the run used the ibguard rule).
   - Compare: 2 to 4 results, curves rescaled to 1 and overlaid, metrics side by side.
6. **Optimize**
   - a base configuration (a saved backtest or the current form); a range for each parameter, prefilled from the schema; objective, minimum trades, mode;
   - a live count of combinations, the estimate and its confirmation, progress;
   - results: a sortable table, the heatmap with its two axes, in-sample against out-of-sample metrics, walk-forward windows with the chained curve;
   - promote a combination.
7. **Live**, one card per market:
   - the portfolio: source backtest, capital, first session, run mode, holdings (symbol, quantity, value, weight);
   - the curve: portfolio, benchmark and replay;
   - the current batch: lines with checkboxes, symbol, action, quantity, kind, limit, reference, status and reason, and the cutoff countdown;
   - [Confirm] opens a dialog that lists the orders, the account kind, and the ibguard exclusions it will make; [Cancel working orders]; [Regenerate]; batch history;
   - create a portfolio from a daily backtest;
   - the ibguard panel: pick a backtest that used the ibguard rule, review the preview, confirm the write.
8. **Settings**: language, logout, data credits (Tiingo, FinMind, FRED) and the ECharts licence, schedule and limits (read-only), saved offline results with a button to clear them.

### 20.3 Charts

- The x axis is time. Every chart has pinch and drag zoom (`dataZoom` inside), a slider, and range buttons: 1M, 6M, 1Y, 5Y, All on the daily timeline; 1D, 5D, 1M on minute timelines.
- Line series use `sampling: 'lttb'`.
- **Colours: green is up, red is down.** **Intentional** (the owner's choice): candles, return heatmaps and positive or negative values. Signs and labels repeat the meaning, so colour is never the only cue.
- Light and dark follow `prefers-color-scheme`, and the charts switch theme to match.

### 20.4 Behaviour

- Poll `GET /api/state` every 2 s while a job runs, otherwise every 15 s. Refresh a view when an event concerns it. Show the login form on 401.
- **Confirmations** before: deleting a list, a result or an optimization; purging a symbol; confirming a batch; cancelling orders; writing ibguard thresholds; deactivating a live portfolio; starting a long optimization.
- **Languages**: zh-TW and en. Every string lives in `locales/zh-TW.json` and `locales/en.json`, flat key to string, fetched by `app.js`. The default follows `navigator.language` (`zh*` selects zh-TW); the choice is kept in `localStorage`, read and written inside try/catch. Every code is localized: statuses, outcomes, job kinds, phases, event kinds, error codes, flags, exit reasons, line statuses and reasons.
- **Layout**: usable from 375 px wide (iPhone) through iPad in both orientations to desktop. Touch targets of at least 44 px. Tables scroll inside their own containers. Nothing depends on hover. Safe-area insets are respected in standalone mode. Numbers use `Intl.NumberFormat`; times show in the market's time zone.

### 20.5 PWA and offline viewing

- `manifest.webmanifest`: name and short name "btlab", `start_url` "/", `scope` "/", `display` "standalone", theme and background colours, PNG icons of 192 and 512 px (one `maskable`). An `apple-touch-icon` of 180 px and the `apple-mobile-web-app-capable` and `mobile-web-app-capable` meta tags. The icons come from a standard-library script (`scripts/make_icons.py`, zlib and struct) and are committed as PNG.
- **Service worker** `sw.js`, scope "/":
  - **Shell**: it pre-caches the app shell (`index.html`, `app.js`, `style.css`, the locales, the vendored ECharts, the icons, the manifest) under `btlab-shell-<version>` and deletes older shell caches on activation. Shell requests are served stale-while-revalidate. A test checks that the shell list matches the static files.
  - **Results cache** `btlab-results`: it holds, per result, the detail, the series and the first 500 trades. Which results it holds comes from `GET /api/offline`: the 20 most recently opened plus every result marked "keep offline". The server owns that choice, so it is tested in Python. While online, the app syncs the cache to that set at startup, after opening a result and after a keep-offline change: it stores missing results and deletes the others.
  - API GETs go to the network. When the network fails, a stored result is served from the results cache. POST, PUT, PATCH and DELETE never use a cache.
- **Offline mode**: when `GET /api/state` fails, the UI shows "Offline — saved results only (as of <time of the last successful state>)". It lists only stored results and disables running, updating, confirming and writing.
- Logout deletes the results cache.
- Service workers need a secure context. The PC (`http://127.0.0.1`) and Tailscale Serve (HTTPS) both qualify.
- Storage of a home-screen web app is not subject to Safari's 7-day eviction of website data, but a Safari tab is. The README tells the owner to open btlab from its home-screen icon.

## 21. Security

- **Listening**: 127.0.0.1 only (§19). iPhone and iPad reach btlab through Tailscale Serve (HTTPS port 10000 → `http://127.0.0.1:8091`) inside the tailnet. Never Funnel, never a public port.
- **Headers** on every response, errors and static files included:
  - `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `Cache-Control: no-store` on `/api/*` and `no-cache` on everything else.
- **CSP exception**: if ECharts cannot render under this CSP even with richText tooltips and the canvas renderer, add `style-src 'self' 'unsafe-inline'` only, and record it in the plan. Never relax `script-src`.
- **Secrets**: the web token, the session value, the Tiingo key, the FinMind token, the FRED key, the spylead and ibguard tokens and the SMTP password never appear in logs, events, API responses, payloads, `last_fetch_error`, emails or error messages. URLs are redacted before any logging (§5.7).
- **Code**: no route accepts code. Plugins load only from the local `strategies/` folder; rules are data.
- **IB**: only `ib_orders.py` places or cancels orders, and test mode never constructs it (§15.1). The client id differs from ibguard's. Tests never connect to any IB port (4001, 4002, 7496, 7497).
- **Dependencies** are locked with hashes:
  - `requirements.in` lists exactly `numpy`, `aiohttp`, `tzdata`, `ib_async==2.1.0` and `pytest`. `ib_async` is pinned because §5.4 and §15 depend on its exact behaviour.
  - `pylock.toml` is generated on the PC with `python -m pip lock -r requirements.in -o pylock.toml`, after upgrading pip inside the venv. The lock is specific to that platform and Python version.
  - Installs use `python -m pip install -r pylock.toml`. The project is not installed as a package.
  - The lock changes only when the owner asks.
- **Git**: `config.toml`, `data/` and `logs/` are git-ignored. `buildctl/` and `.claude/settings.json` are never ignored.

## 22. Configuration and CLI

`python -m btlab --config <path>`

`config.toml` is git-ignored; `config.example.toml` is committed.

```toml
mode = "test"                 # "test": live orders are virtual. "trade": confirmed orders go to IB.

[web]
port = 8091                   # always bound to 127.0.0.1; publish it with Tailscale Serve
token = "change-me"           # random, at least 16 characters

[tiingo]
api_key = ""

[finmind]
token = ""

[fred]
api_key = ""

[spylead]
url = "http://127.0.0.1:8090" # empty disables the import
token = ""

[ibguard]
url = "http://127.0.0.1:8080" # empty disables coordination
token = ""

[ib]
host = "127.0.0.1"            # empty disables IB: no minute bars, no orders
port = 4002                   # IB Gateway paper; live is 4001
client_id = 31                # keep stable; must differ from ibguard's 17
account = ""                  # required when the login has several accounts

[minute]
backfill_years = 3

[orders]
us_order_kind = "MOO"         # "MOO" or "LIMIT"
us_limit_offset_pct = 1.0
tw_limit_offset_pct = 3.0

# [limits]                    # required in trade mode, with your own numbers (§15.3)
# us_max_orders_per_day = ...
# tw_max_orders_per_day = ...
# max_order_pct_of_equity = ...
# us_max_order_value = ...     # USD
# tw_max_order_value = ...     # TWD

[schedule]
enabled = true
us_update = "08:30"           # Asia/Taipei, Tuesday to Saturday
tw_update = "18:00"           # Asia/Taipei, Monday to Friday

[email]                       # optional; an empty smtp_host disables email
smtp_host = ""
smtp_port = 587
username = ""
password = ""
sender = ""
recipient = ""
language = "zh-TW"            # or "en"
```

- Unknown keys, missing keys and wrong types are startup errors that name the key and never echo a value.
- `mode` is required. `web.token` must not be the placeholder and must have at least 16 characters.
- Empty API keys and tokens are allowed at startup, with a warning; requests then fail with MISSING_KEY.
- Trade mode needs `[limits]` with all five keys above 0. `ib.client_id` must not be 17.
- `taxes.toml` and `strategies/` sit next to `config.toml` in the repository root. `data/` and `logs/` are created there.
- Lists, rules, results and live portfolios live in the database, not in the config file.

## 23. Runtime

- **Startup order**: config, tax table, the process lock, both stores, plugins, the web server, the scheduler (with its catch-up check), then ready. The IB link and the ibguard and spylead clients connect on demand. Exclusions left ACTIVE trigger the safety sweep of §16.2 as soon as IB and ibguard are reachable.
- **One process per database**: at startup btlab takes an operating-system lock on `data/btlab.lock` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), holds it for the life of the process, and exits before opening a database or the port if another process holds it. The lock dies with the process; the file's presence means nothing.
- **Logging**: console at INFO, plus a rotating file `logs/btlab.log` at DEBUG (5 MB, 5 backups).
- **Shutdown on Ctrl+C**: stop accepting requests, interrupt jobs (§17.1), shut the pool down, disconnect IB (orders at IB stay), close the stores.
- **Keep awake**: on Windows, `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while a job runs or a btlab order works, cleared afterwards. No-op elsewhere.
- **Unexpected exceptions** in a handler or job are logged with their traceback, recorded as INTERNAL_ERROR, and never stop the process.
- **Autostart**: `scripts/install-autostart.ps1` registers a per-user logon task named `btlab`, and `-Remove` deletes it. It must work in Windows PowerShell 5.1.
  - The task runs a launcher written to `%LOCALAPPDATA%\btlab\launch.ps1`, on the system drive. It waits up to 10 minutes for the project folder, because the project lives on an external drive, then starts `.venv\Scripts\pythonw.exe -m btlab --config config.toml` in that folder.
  - If registering the task is refused, the script puts the launcher in the user's Startup folder instead, and says so.
- **Time**: UTC internally. The app takes a clock callable, so tests control time. Market time zones come from `zoneinfo`, which needs `tzdata` on Windows.

## 24. Testing and acceptance

### 24.1 Rules

- pytest only. No network, no real IB, no real time: inject the clock, the sleep, the HTTP session and the ports.
- **Pure modules**: table-driven tests on small hand-made data, plus the truncation property tests named below (altering or removing bars after bar c changes nothing at bars up to c).
- **HTTP clients**: aiohttp's test server or an injected fake session, with hand-written JSON samples. Never real downloads.
- **IB**: fakes of `HistoryPort` and `OrderPort`. No test imports `ib_async`.
- **ibguard and spylead**: aiohttp test servers that mimic their documented APIs.
- **Jobs**: fake sources, temporary databases, the injected clock, and a synchronous stand-in for the pool.
- **Web**: aiohttp's test utilities.
- **Speed**: keep test data small. Production-size timing belongs to `python -m btlab.bench`, which the suite never runs.

### 24.2 Acceptance criteria

| Id | Criterion |
|---|---|
| AC-1 | Config: a valid file loads; a missing file, an unknown key, a missing key, a wrong type, the placeholder or a short token, `client_id = 17`, and trade mode without five positive limits are errors that name the key and never echo a value; empty keys only warn. `taxes.toml`: overlaps, gaps and unknown categories are errors naming the entries; lookups give BOND_ETF 0.1 on 2016-12-31, 0.0 on 2017-01-01 and 0.1 on 2027-01-01, and a day trade in a STOCK 0.3 on 2017-04-27, 0.15 on 2017-04-28 and 0.3 on 2028-01-01. |
| AC-2 | Store: schema creation is idempotent for both files; every table round-trips; upserting an overlap window replaces only those days; minute bars and coverage live in `minute.db`; the WAL fallback works with a patched pragma; a write against a locked database fails within the busy timeout with the store error type. |
| AC-3 | Sources: each request's path, parameters and headers (Tiingo key in a header, FinMind bearer token, FRED key in the query); translation of hand-written payloads, including FinMind's zero-close rows and FRED's `"."`; retries and `Retry-After` with an injected sleep; no retry on 404; MISSING_KEY without a request; the Tiingo (45 an hour, 900 a day) and FinMind (550 an hour) budgets, persisted across a restart; MAINTENANCE; no key, token or URL in any error or log line. |
| AC-4 | Actions and adjusted prices: examples A1 to A6 of §6.3; the TW dividend rules of §6.2 (`息` with and without a policy row, `權`, `權息` with and without D); split, par-value and capital-reduction ratios; bad actions skipped with an event; adjusted volumes. |
| AC-5 | Calendars and tradability: TW days from FinMind, US days from SPY plus weekdays; alignment carries closes over gaps and no-trade days and marks them untradable; NaN outside a symbol's range; the limit lock with 7% or 10% by date and the 0.9 margin blocks only the locked direction; orders wait for the next tradable bar. |
| AC-6 | Indicators and features: every indicator of §8.1 matches hand-computed values (Wilder seeds, EMA seed, MACD signal seed, DONCHIAN without the current bar, population and sample deviations); warm-up NaN; truncation invariance for every indicator and feature; the features of §8.2 and §8.3 on hand-made minute data; daily values on minute timelines come from the previous session (§8.4). |
| AC-7 | Engine accounting: equity = cash + long − short on every bar; NEXT_OPEN and SAME_CLOSE fills with slippage; reducing orders before increasing ones; pro-rata scaling when buying power is short; lots and skipped sub-lot orders; US commissions (per share, minimum, and the 1% cap below the minimum) and the TW minimum; TW taxes by category and date, with the day-trade rate for same-session round trips in both directions; trade P&L net of everything attributable; partial reductions at average cost. |
| AC-8 | Shorting: shorts only with `allow_short`, otherwise flat with SHORTS_IGNORED; rotations never short; buying power with m = 0.5 and 0.9; borrow fees by calendar days; US shorts pay dividends; the TW short-sale fee; forced cover at the first tradable bar on or after a suspension's start, and no short sale through its end. |
| AC-9 | Corporate actions in REALISTIC mode: a share event changes quantities, levels and pending targets and preserves value except the cash in lieu; cash dividends are credited net of withholding to longs and debited to shorts; on an ex-date whose open equals the reference price, equity is unchanged by the action. ADJUSTED mode applies no action, and its buy-and-hold return without costs equals the adjusted-price return. |
| AC-10 | Exits and paths: each overlay on hand-made bars (levels, fills at the level, gap fills at the open, max-hold timing under both fill timings); the daily path for up and down bars; the minute path with a gap between minutes; the earliest crossing wins and stops win ties; the ibguard vectors L1 to L10 and S1 to S5 of §10.8, fed as path points, give the same `ref` values and exits; re-entry only after the strategy's target changes; PATH_FALLBACK counts. |
| AC-11 | Metrics: every metric of §12 against hand-computed values on a small curve and trade list (P from calendar days, rf from forward-filled DTB3, Sortino's downside, an unrecovered drawdown, α and β); undefined values are null; the CSV exports have the columns of §12 and a BOM; the benchmark runs as BUY_HOLD through the engine and is flagged BENCHMARK_PARTIAL when partial. |
| AC-12 | Built-ins and sizing: each strategy A to J gives the expected targets on synthetic series made to trigger its rules (state and reversals, period starts, `abs_filter`, safe asset or cash, vol-inverse weights summing to selected/top_k); every sizing mode; quantities held without drift rebalancing; the `max_position_pct` cap; truncation invariance of every built-in. |
| AC-13 | Plugins: both styles load from a temporary folder; `PARAMS` validation; broken plugins are listed with their errors while the others load; `ctx` exposes only rows up to t as read-only views; the look-ahead check flags a plugin that peeks one bar ahead and passes an honest one; plugins receive no store, config, IB or network object; the two examples load and run. |
| AC-14 | Rule builder: every validation code with its path; 3 levels pass and 4 fail; 33 conditions fail; parameter substitution; every operator, `within` and `for`; NaN is false; a rule equivalent to built-in A gives identical directions; `"tf": "1d"` on a minute timeline uses the previous session; features on the daily timeline; minute operands on the daily timeline fail with TF_MISMATCH; truncation invariance. |
| AC-15 | Intraday: 1-minute bars resample to 5, 15, 30 and 60 minutes aligned to the open, with a short last bucket and skipped gaps; fills at the next bar within the session and at the next session's first bar; close-out m minutes before the end; the TW day-trade tax; the PDT flag only before 2026-06-04 and below USD 25,000; NO_MINUTE_DATA; metrics on session-end equity. |
| AC-16 | Optimization: lexicographic enumeration and the 2,000 limit; `min_trades`; ties to the earlier combination; the heatmap fixes the other parameters at the best; the 70% split; walk-forward windows of 3, 1 and 1 years with the chained curve, NO_RESULT windows in cash, and a short last window dropped; the estimate and NEEDS_CONFIRMATION; a cancel keeps partial results; identical results with the in-process stand-in and with a real pool of 2 workers on a tiny case. |
| AC-17 | `python -m btlab.bench` builds synthetic data and prints the time of each case of §24.3. The suite only imports it. |
| AC-18 | Jobs: one job per lane (409); progress; cancel and interruption rules; update outcomes (UP_TO_DATE without inserts, UPDATED, PARTIAL, FAILED, MAINTENANCE); the follow-ups in order, and LIVE_DATA_INCOMPLETE; due times of both schedules in Asia/Taipei on the right weekdays; one catch-up run after downtime; retries after failures; the MAINTENANCE retry at 07:05; MINUTE pauses while DATA runs. |
| AC-19 | Web security: every `/api/*` route except login requires the session; 415, INVALID_JSON, and non-finite or boolean numbers are rejected; the §21 headers are on an API response, an error and a static file; the server listens on 127.0.0.1 only; cookie flags, with `Secure` under `X-Forwarded-Proto: https`; each configured secret is absent from every captured log line, event, response, payload and error. |
| AC-20 | Web routes: the success and error paths of every route in §19, including JOB_RUNNING, IN_USE, NEEDS_CONFIRMATION and TOO_MANY_COMBOS; the state document's shape; series and candle payloads (time units, 6 significant digits, null for NaN, 5,000-bar windows); the CSV routes; compare with 2 to 4 results; `GET /api/offline` (AC-22). |
| AC-21 | UI static checks: both locale files define the same keys and cover every code the backend can emit; no `innerHTML`, `insertAdjacentHTML`, `document.write`, inline script, `style` attribute or inline event handler outside `vendor/`; the manifest is valid and its icons exist as PNG of the stated sizes; the ECharts file matches its SHA-256; the chart colour constants make up green and down red. |
| AC-22 | Offline: `GET /api/offline` returns the 20 most recently opened results plus every keep-offline result, and opening a result updates its `opened_at`; the shell list in `sw.js` equals the static files it must cache; `sw.js` serves only GET requests from its caches; logout deletes the results cache; the offline strings exist in both locales. |
| AC-23 | spylead: the client's calls (login, state, SPY, KEPT series, F when OK) against a fake server; a snapshot replaces the set in one transaction; an unreachable spylead keeps the previous snapshot and records an event; alignment by calendar day; IN_SAMPLE_SERIES on every backtest and optimization that uses F or COMPOSITE, whether through a rule or `bars.ext()`. |
| AC-24 | IB minute bars: the request of §5.4 through the fake `HistoryPort`; at least 2 s between requests on the injected clock; the pacing retry; normalization (factor near 1, near the split product, mismatch); coverage states and resumption; NO_PERMISSION stops the market; the connection uses the configured client id, never 17, and is read-only in test mode; only `ib_link.py` and `ib_orders.py` import `ib_async`. |
| AC-25 | Live portfolios in test mode: creation rules (daily only; one active per market and mode); replay targets including pending orders; live lots (US 1; TW the size increment or 1,000); skips; batch replacement; expiry at the cutoff; virtual fills (MOO at the open, limit rules, expiry on untradable or locked bars, costs); corporate actions on the virtual book; ledger rows and curves; test mode never constructs the order adapter and connects read-only. |
| AC-26 | Trade-mode safety: startup refuses without limits; limits at generation and before sending (value, percent, count keeping the largest lines); the send function's refusals; order fields through the fake `OrderPort` (MOO as MKT with OPG; LIMIT DAY with offsets, clipping and market-rule rounding; `outsideRth` false; account; a unique `btlab:` orderRef); reducing lines first; BATCH_STALE; line states and ledger fills; recovery by exact orderRef; foreign orders untouched; cancel only btlab's own working orders; `placeOrder` and `cancelOrder` appear only in `ib_orders.py` and `reqGlobalCancel` nowhere; READ_ONLY_API stops the batch. |
| AC-27 | ibguard: the preview lists exactly the STK positions that would exit (long and short, take-profit and stop-loss, unknown prices) with account and paused state; a changed affected list changes the token and gives PREVIEW_CHANGED; the write sends STK only, within ibguard's ranges; the sell protocol: skips while ibguard exits or is unreachable, keeps the owner's exclusions, excludes then verifies then sends, restores when the order ends with a position left, CLOSED when flat, sweeps after the session and at start, and retries failed includes with one event and one email. |
| AC-28 | Runtime: a second process exits before opening a database or the port; a lock left by a killed process does not block a restart; startup and shutdown order; logging; the autostart script (task `btlab`, launcher under `%LOCALAPPDATA%\btlab`, `-Remove`, the Startup-folder fallback, waiting for the folder); the notifier raises nothing on SMTP failure and is off with an empty host. |
| AC-29 | Determinism: identical backtests, optimizations and live generations give identical results. |
| AC-30 | Flags: every flag of §10.11 arises in a scenario test, is stored with its result and is served by the API. |

### 24.3 Bench and checks on the PC

`python -m btlab.bench` uses synthetic data and the real pool:

| Case | Data | Target |
|---|---|---|
| 1 | One daily run: 50 symbols × 30 years, MA_CROSS with the ibguard exit, REALISTIC with synthetic dividends and splits | ≤ 5 s |
| 2 | A grid of 2,000 combinations of case 1 | ≤ 10 min |
| 3 | One minute run: 10 symbols × 3 years of 1-minute US bars, a rule with daily operands and close-out | ≤ 60 s |

Manual, by the owner:

1. Install from `pylock.toml` and run the full suite on Windows.
2. Fill `config.toml`, start the app, update both markets. Update again: "already up to date". Compare a few closes with Tiingo's and FinMind's sites, and one split or dividend in raw against adjusted candles.
3. Run the bench and compare with the targets.
4. The UI checklists in the plans of prompts 17, 18 and 19: Edge installed as an app, an iPhone at 375 px and an iPad in both orientations through Tailscale Serve, added to the home screen. Offline: disconnect, then open saved results from the home-screen icon.
5. IB, paper or read-only: backfill a few days of one US symbol and check its coverage; try one TW symbol (OK or NO_PERMISSION).
6. Test mode: create a US live portfolio from a daily backtest; after the next update a batch appears; confirm it; after the following update, check the virtual fills and the paper curve.
7. Trade mode on the paper account: set your limits, `mode = "trade"`, IB Gateway on paper with "Read-Only API" unchecked, ibguard on its paper database. Confirm a small batch and find the `btlab:` orders in TWS. Sell a position ibguard protects, then cancel the working order before the open: ibguard excludes the position, then restores it, and the email arrives.
8. Install the autostart, sign out and in, and check that btlab comes up after the drive mounts.

## 25. Accepted limitations

Owner decisions. All intentional.

1. **Survivorship bias**: lists hold only symbols that exist today, so results are biased upward.
2. **Intrabar order** is assumed from the daily bar (§10.8) wherever minute bars are missing; minute bars cover only chosen symbols and about 3 years.
3. **Fills** happen at the open or close with a fixed slippage: no partial fills, auction imbalances or market impact. Test-mode limit orders fill at the open or at the limit.
4. **Corporate actions** are approximated in places: capital-reduction refunds count as share value; fractional shares are paid at the reference price; `權息` without a policy row becomes a share ratio; dividends arrive on the ex-date, not the pay date.
5. **The TW limit lock** is detected by a heuristic (§6.5).
6. **Shorting** is simplified: no uptick rule, no recalls, a constant borrow fee, no margin calls, and TW shorts are covered at the start of a suspension.
7. **Cash** earns no interest; there is no margin borrowing; each backtest has one currency.
8. **ADJUSTED mode** reinvests dividends untaxed and applies lots and minimum commissions to adjusted prices.
9. **Costs** omit IB's Taiwan clearing fees and tier changes, US regulatory fees and the rounding of Taiwan's tax. TW defaults assume IB; a Taiwan broker's commission (for example 0.1425% × discount, minimum NT$20) can be entered per backtest.
10. **IB and Taiwan**: IB does not offer Taiwan market access to residents of Taiwan. TW orders and TW minute bars through IB may therefore be unavailable; btlab detects it at run time (NO_PERMISSION, rejected orders), and TW live portfolios still work in test mode.
11. **Live portfolios** run daily strategies only and trade at the next open. A strategy's intrabar exits are not executed live; ibguard protects positions in real time. Live holdings drift from the backtest through rounding, skipped lines and real fills, and TW live orders use IB's lot size.
12. **spylead's F and composite** are in-sample (§5.6).
13. **The PDT flag** is informational and stops at 2026-06-04.
14. **Optimization can overfit**; the out-of-sample check and walk-forward only measure it.
15. **Data licences**: Tiingo for internal use; FinMind for non-commercial use with attribution; FRED's terms of use.
16. **The PC** must be on, with btlab running, for the iPhone and iPad to use it; offline they show saved results only. Without the external drive btlab cannot start.
17. **US holidays** after the last stored SPY bar are unknown, so scheduling treats every weekday as a trading day.
18. **Plugins** run with the owner's privileges, because they are the owner's code.

## 26. Extension points and refactor seams

Extension points:

- **Daily sources**: a new client that returns bars and the normalized actions of §6.2.
- **Minute sources**: `HistoryPort`. FinMind Sponsor (Taiwan minute bars) or a Taiwan broker API could implement it (TODO.md).
- **Orders**: `OrderPort`. A Taiwan broker API could implement it (TODO.md).
- **Strategies**: plugins; the built-in registry; the operand registry (indicators, features, external-series providers).
- **Costs**: one commission function per market; `taxes.toml`.
- **Metrics**: one function per metric; the list of objectives.
- **Notifications**: the notifier protocol (for example Telegram or push).
- **API**: the JSON API can serve a native client.

Refactor seams:

- **Layers**: pure core (markets, prices, calendar, indicators, features, sizing, builtins, rules, costs, exits, engine, intraday, metrics, live, scheduler); orchestration (backtest, optimize, jobs, trading); adapters (store, HTTP clients, ib_link, ib_orders, ibguard_client, spylead_client, notifier, web).
- **Stores**: synchronous SQLite behind one class per file, so either can move to a thread.
- **Pool**: injected.
- **Run mode**: decided once, in the wiring of `__main__.py`. `trading.py` sees only which `OrderPort` it was given.
- **Polling** lives in `web.py` and `app.js`; it can become SSE or WebSocket without touching the jobs.

Deferred items live in `TODO.md`.

## 27. Owner setup

The README carries these.

- **Keys**: a free Tiingo API token; a FinMind token (register and verify the email address); a free FRED API key. Put them in `config.toml`, with the web tokens of spylead and ibguard.
- **Python** 3.12 or later on Windows with the `py` launcher.
- **Drive**: the project lives on `E:\00_CODING\btlab`. Give the external drive a fixed letter in Disk Management. On exFAT, Git needs `safe.directory` entries for the folder.
- **Tailscale**: `tailscale serve --bg --https=10000 http://127.0.0.1:8091` publishes btlab inside the tailnet (spylead uses 8443). Open `https://<pc-name>.<tailnet>.ts.net:10000` on the iPhone or iPad and add it to the home screen. Never use Funnel.
- **Edge**: open `http://127.0.0.1:8091`, then Apps, "Install this site as an app".
- **IB Gateway**:
  - Minute bars need market-data permissions for the market (US data, and Taiwan data where IB offers it). Any login works for history, including paper and "Read-Only API".
  - Trade mode needs "Read-Only API" unchecked and the socket port equal to `ib.port`. btlab's client id (31) must differ from ibguard's (17).
  - While Claude Code builds or changes btlab on this PC, IB Gateway is closed, logged in to paper, or set to "Read-Only API".
- **Trade mode**: set `[limits]` with your own numbers, then `mode = "trade"`. Keep ibguard running so sells are coordinated.
- **Backup**: copy `data/` while btlab is stopped.
