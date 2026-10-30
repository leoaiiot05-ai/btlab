# Prompt 3: Data sources

Covers SPEC.md §4.1, §5.1, §5.2, §5.3, §5.7, §6.2 (the raw inputs of the actions), §7 (symbols and categories) and the secrets rule of §21. Acceptance: AC-3.

## 1. Steps

1. **`btlab/records.py`** (pure) gains the day conversions and the typed TW rows.
   - `to_day(date) -> int` and `to_date(day) -> date`: days since 1970-01-01 (§3). calendar.py (prompt 4) and the clients share them.
   - Typed FinMind rows, which prices.py (prompt 4) normalizes into actions without importing a client:
     - `StockInfo(symbol, name, exchange, industry)`: TaiwanStockInfo, each stock's latest row; exchange TWSE or TPEX, None for another type;
     - `DividendResult(day, before_price, after_price, kind)`, where kind is `息`, `權` or `權息`, kept as given;
     - `DividendPolicy(cash_ex_day, cash)`: one TaiwanStockDividend row, with `cash = CashEarningsDistribution + CashStatutorySurplus`, and None when the row has no cash ex-date;
     - `SplitPrice(day, symbol, type, before_price, after_price)` and `ParValueChange(day, symbol, before_close, after_ref_close)`: market-wide tables; the TW update filters them by symbol;
     - `CapitalReduction(day, closing_price, reference_price, reason)`;
     - short suspensions use `ShortSuspension` (prompt 2).
2. **`btlab/markets.py`** (pure) gains the symbol rules of §7:
   - `normalize_symbol(market, text) -> str`: stripped and uppercased, then checked against US `^[A-Z][A-Z0-9]{0,5}(\.[A-Z])?$` or TW `^[0-9]{4,6}[A-Z]?$`. A mismatch raises `ValueError` naming the market, for the web layer's INVALID_FIELD (prompt 15).
   - `category_by_rule(market, symbol) -> str`: TW `BOND_ETF` when the code starts with `00` and ends with `B`, `ETF` when it starts with `00`, `TDR` when it has six digits and starts with `91`, otherwise `STOCK`; US `STOCK`.
3. **`btlab/http.py`**, the request rules of §5.7:
   - **`SourceError(code, text)`**, with `code` one of TIMEOUT, `HTTP_<status>`, BAD_PAYLOAD, MISSING_KEY, SYMBOL_UNKNOWN, RATE_LIMITED, MAINTENANCE, UNREACHABLE (`SOURCE_ERROR_CODES` lists them, `HTTP_` as a prefix). `str(error)` is `"<code>: <text>"`, the form stored in `last_fetch_error`. The constructor removes URLs from the text and applies `redact`, so even a FinMind `msg` that quotes a link holds none.
   - **`redact(text)`**: masks the values of `api_key`, `apikey`, `*token` and `Authorization` (with a `Token` or `Bearer` scheme) in query strings, headers, dict reprs and JSON. Case-insensitive.
   - **`RequestBudget(limits, times=(), on_record=None)`**: `limits` are `(seconds, count)` pairs; `times` is the persisted log (UTC epoch seconds).
     - `wait(now)`: the seconds until one more request fits every rolling window, 0 when it fits now.
     - `record(now)`: appends `now`, drops times older than the longest window, and passes the log to `on_record`. The job passes `store.save_request_log` there, so the log is persisted after every request and a restart reloads it with `store.request_log` (§5.1, §5.2).
     - `times`: the current log.
   - **`Requester(source, session, *, budget=None, sleep=asyncio.sleep, clock=time.time, on_wait=None)`**: one source during one job. The aiohttp session is passed in (one per job). `source` names the service in texts and logs.
     - `get_json(label, url, params, headers=None, error_text=None)`. `label` describes the request for logs and errors, for example `prices of BRK-B from 2026-10-21 to 2026-10-28`; no log line or error ever holds the URL.
     - Before each attempt: wait for the budget, calling `on_wait(until)` and the injected `sleep` until it fits (the job shows "waiting … until 14:05", AC-18); then record the attempt. Every attempt counts, because the provider counts it.
     - One attempt: `session.get(url, params=..., headers=..., timeout=ClientTimeout(total=30))`, reading the body as bytes.
     - 200: the body parsed as JSON, or BAD_PAYLOAD.
     - Timeouts (TIMEOUT), connection errors (UNREACHABLE) and 5xx (`HTTP_5xx`): 3 retries after 2, 4 and 8 s, then the last error.
     - 429: wait `Retry-After` seconds (at most 60, 60 when absent or not a number) and retry once; a second 429 is RATE_LIMITED.
     - Other statuses: no retry, `HTTP_<status>`. `error_text(payload)` may supply the text from a JSON error body (FinMind's `msg`).
     - Logging: DEBUG per attempt, WARNING per retry, INFO per budget wait, each with the source and the label only.
   - **`fetch_start(data_start, last_stored_day, overlap_days) -> int`**: the first day to request: `data_start` on the first fetch, else `overlap_days` before the latest stored day, never before `data_start`. Tiingo and FinMind prices use 7, FinMind's corporate actions 400 (§5.1, §5.2), FRED 30 (§5.3).
   - Shared payload helpers that raise BAD_PAYLOAD naming the dataset, the row number and the field: `field(row, key, where)`, `number(row, key, where)` (int or float, not bool), `day(row, key, where)` (the date part of `YYYY-MM-DD…`).
4. **`btlab/tiingo.py`** (§5.1):
   - `LIMITS = ((3600, 45), (86400, 900))`; `tiingo_ticker(symbol)` maps `BRK.B` to `BRK-B`.
   - `TiingoClient(requester, api_key)`, `await daily(symbol, first_day, last_day) -> (DailyBars, list[Action])`: `GET https://api.tiingo.com/tiingo/daily/<ticker>/prices` with `startDate`, `endDate`, `format=json`, `resampleFreq=daily` and the header `Authorization: Token <key>`. An empty key is MISSING_KEY before any request or budget wait. HTTP 404 becomes SYMBOL_UNKNOWN, without a retry.
   - `translate_prices(rows)`, pure: raw OHLCV by day (date part of `date`), sorted by day; `divCash > 0` gives `CASH_DIV(cash=divCash)` with detail DIV_RESULT; `splitFactor ≠ 1` gives `SHARE_RATIO(ratio=splitFactor)` with detail SPLIT above 1 and REVERSE_SPLIT below. Adjusted fields are ignored. BAD_ACTION checks belong to prices.py (prompt 4).
5. **`btlab/finmind.py`** (§5.2):
   - `LIMITS = ((3600, 550),)`; `in_maintenance(now)`: Sunday 00:00 to 06:59:59 Asia/Taipei.
   - `FinMindClient(requester, token)`: `GET https://api.finmindtrade.com/api/v4/data` with `dataset`, and `data_id`, `start_date`, `end_date` as the table of §5.2 says, and the header `Authorization: Bearer <token>`. An empty token is MISSING_KEY, and a request inside the maintenance window is MAINTENANCE, both before any request. A body `status` other than 200 is `HTTP_<status>` with FinMind's `msg`; so is an HTTP error status whose JSON body has a `msg`.
   - Methods: `trading_days()`, `stock_info()`, `prices(symbol, first, last)`, `dividend_results(...)`, `dividend_policies(...)`, `split_prices(first, last)`, `par_value_changes(first)` (start date only), `capital_reductions(symbol, first, last)`, `short_suspensions(symbol, first, last)`.
   - Pure translators, one per dataset: trading days (sorted int64); stock info (latest row per stock by `date`); prices (`close = 0` rows are no-trade days: NaN prices, the reported volume, `no_trade` set); dividend results; policies (an empty `CashExDividendTradingDate` gives None); splits and par-value changes with their `stock_id`; capital reductions; short suspensions. Extra fields are ignored; a missing needed field is BAD_PAYLOAD.
6. **`btlab/fred.py`** (§5.3): `FredClient(requester, api_key)`, `await dtb3(first_day) -> RiskFreeRates`: `GET https://api.stlouisfed.org/fred/series/observations` with `series_id=DTB3`, `api_key`, `file_type=json` and `observation_start`. The key goes in the query string because FRED requires it; the URL never reaches a log or an error. `translate_observations(payload)`: `"."` values are dropped (absent days); another non-number is BAD_PAYLOAD.
7. **`btlab/__main__.py`**: `setup_logging` formats every line through `redact`, tracebacks included (the refactor seam named in prompt 1). This is a second guard: the clients never log a URL or a key.
8. **Tests** (no network: an injected fake session; one test runs `Requester` through a real `aiohttp.ClientSession` against aiohttp's test server on 127.0.0.1, which §24.1 allows, to check the real API calls):
   - `tests/fakes.py`: `FakeSession` (records each call's URL, params, headers and timeout; answers with scripted statuses, headers and bodies, or raises), and `FakeTime` (an epoch-seconds clock that the injected sleep advances, recording each sleep).
   - `tests/test_http.py`: SourceError's form and scrubbing; `redact` on URLs, header reprs and JSON; the budget's rolling windows, waits and pruning, and a restart that reloads the log from a temporary `Store`; `Requester`: success, the 30 s timeout, 3 retries after 2, 4 and 8 s for timeouts, connection errors and 5xx, then the error; 429 with `Retry-After` (capped at 60, default 60) once, then RATE_LIMITED; no retry on 404 or 400; BAD_PAYLOAD for a body that is not JSON; the budget wait with `on_wait` and the sleep; log lines without URLs; `fetch_start`; the aiohttp round trip.
   - `tests/test_tiingo.py`, `tests/test_finmind.py`, `tests/test_fred.py`: each request's URL, parameters and headers (the key only in FRED's query string, never in a Tiingo or FinMind URL); MISSING_KEY with no request; Tiingo's 404 as SYMBOL_UNKNOWN after one request; the Tiingo budget (the 46th request in an hour and the 901st in a day wait) and the FinMind budget (the 551st); MAINTENANCE at its boundaries; every translator on hand-written JSON, including zero-close rows, FRED's `"."`, the latest TaiwanStockInfo row and BAD_PAYLOAD for a missing field; and, per client, a sweep of failures whose error texts and captured log lines hold no key, token or URL (AC-3).
   - `tests/test_markets.py`: `normalize_symbol` and `category_by_rule`.
   - `tests/test_cli.py`: a log line that quotes a URL with `api_key` is written redacted.
9. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **US cash dividends get the detail DIV_RESULT.** §6.2 lists no detail for Tiingo's `divCash`; DIV_RESULT is the listed detail of a dividend's ex-date, with `ref_before` and `ref_after` left empty.
- **FinMind and unknown codes.** FinMind answers an unknown `data_id` with no rows rather than an error. The TW update (prompt 14) fails such a symbol with SYMBOL_UNKNOWN when TaiwanStockInfo does not list it; the client only reports what FinMind says.
- **TIINGO_SYMBOL_BUDGET** (500 distinct symbols a month, §5.1) is counted by the US update in prompt 14, which knows the symbols of each update and can persist the month's set in `settings`.
- **`Retry-After`** is read as seconds; any other form waits the 60 s default, which is within §5.7's "at most 60 s".

## 2. Extension points

- **A data source** is a client class over a `Requester`, plus pure translators into records (§26). spylead and ibguard (prompts 20 and 23) use the same `Requester`; they will add a POST counterpart of `get_json` there.
- **A budget** is a tuple of `(seconds, count)` limits.
- **An error code** is one entry in `SOURCE_ERROR_CODES`, which prompt 17's locale test reads.
- **A redacted field** is one more name in the `redact` pattern.

## 3. Refactor seams

- Network calls and translation are separate: each client method fetches and then calls a pure translator that the tests call directly.
- `Requester` owns every request rule; the clients hold only endpoints, parameters and translation.
- The session, sleep and clock are injected, so the jobs (prompt 14) wire real ones and tests wire fakes.
- `to_day` and `to_date` in records.py can replace the private conversions in taxes.py.

## 4. Deferred items (appended to TODO.md)

- `Retry-After` as an HTTP date; today it counts as the 60 s default (Operations). Seam: `_retry_after` in `http.py`.

## Result

- **AC-3 holds.** `.venv/bin/python -m pytest -q`: 335 passed (also with `-X dev -W error`).
- **Built as planned**, plus:
  - `SourceError` accepts only the codes of §5.7 (`HTTP_<digits>` for statuses), so a typo cannot create a code that the UI cannot localize.
  - FinMind's `msg` is quoted in errors without the token, should FinMind ever echo it; Tiingo's and FRED's error bodies are never quoted.
  - A URL in a quoted message becomes `<link>`; the URL pattern stops before brackets, quotes and a sentence's last punctuation.
- **No real connection.** The clients' tests use `tests/fakes.py`. One test runs `Requester` through aiohttp's own `ClientSession` against `aiohttp.test_utils.TestServer` on 127.0.0.1 (§24.1), so the real aiohttp calls (`get` with `params`, `headers` and `timeout`, `read`, the `Retry-After` header) are checked too.
- **For prompt 14:** build one `RequestBudget(tiingo.LIMITS | finmind.LIMITS, store.request_log(source), partial(store.save_request_log, source))` per job and pass it to `Requester` with the job's `on_wait`; compute windows with `fetch_start` and the clients' `*_OVERLAP_DAYS`; count TIINGO_SYMBOL_BUDGET and FinMind's SYMBOL_UNKNOWN (absent from TaiwanStockInfo) there.
