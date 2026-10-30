# Prompt 20: spylead import

Covers SPEC.md §5.6, §5.7, §8.5, §8.6, §9.5 (`bars.ext`), §9.6 (series operands), §10.11 (IN_SAMPLE_SERIES), §17.1, §17.2 and §19 (`GET /api/spylead`, `POST /api/spylead/snapshot`, the state document). Acceptance: AC-23.

## 1. Steps

1. **`btlab/http.py`**: the requester learns POST with a JSON body and reports the cookies an answer sets (`send(method, …)` returning the body and the cookies; `get_json` stays as it is for the other clients). Any 2xx is success, and an empty body (204) is `None`. Same timeout, retries, Retry-After and redaction as before. `tests/fakes.py`'s `FakeSession` answers through `request(method, …)`, records the method and the JSON body, and its responses carry the cookies of a `Set-Cookie` header.
2. **`btlab/spylead_client.py`**: `SpyleadClient(url, token, requester).snapshot()`:
   - an empty token fails with MISSING_KEY before any request;
   - `POST /api/login {"token"}`; the session cookie it sets is kept in the client object only and sent as a `Cookie` header (aiohttp's cookie jar refuses cookies from an IP host such as `127.0.0.1`, spylead's default);
   - `GET /api/state`, `GET /api/charts/spy`, `GET /api/charts/series/{name}` for each series whose status is `KEPT`, and `GET /api/charts/f` when `f.status` is `OK`;
   - pure translation into `ExternalSeries`: `SPYL1` (`rt`), `LEADL1_<name>` (`rt`), `F` (`f`) and `COMPOSITE` (`composite`), the last two in-sample; each `{first_day, values}` with `null` as NaN;
   - errors are `SourceError`s whose text names the request (`login`, `state`, `series HYG`…), never the URL, the token or the cookie.
3. **Jobs** (`jobs.py`):
   - `Jobs(…, spylead=ServiceConfig)`; spylead is configured when its URL is not empty;
   - the SNAPSHOT phase: the client's series replace the stored set in one transaction (`store.replace_external_series`); when spylead fails, the previous snapshot stays and an event SPYLEAD_UNREACHABLE (WARNING) records the error;
   - `SPYLEAD_SNAPSHOT` on the DATA lane (`Jobs.snapshot()`): UPDATED with the number of series, or FAILED with the error;
   - every data update runs SNAPSHOT after RATES when spylead is configured; a spylead failure there leaves the update's outcome alone.
4. **External series in runs**:
   - `View` carries the stored series (`external`) and a `series(name)` method that aligns one to the view's bars: a bar on market date d takes calendar day d's value, NaN outside the series, on the daily and the minute timelines alike (§5.6, §8.5). The view remembers which series a strategy read.
   - rules read `{"series": NAME}` through it, and plugins through `bars.ext(name)`;
   - `load_run` loads the stored series into `RunData`; `view()` and `minute_view()` pass them on;
   - `run_backtest` sets IN_SAMPLE_SERIES, with the in-sample names read, when the strategy read F or COMPOSITE; an optimization merges its runs' flags, so it carries the flag too;
   - validation knows the stored names: `validate_config(…, series)` and `parse_request(…, series)` pass them to `validate_rule`, as do the rule routes; `GET /api/indicators` lists them as `series` operands, with `in_sample`.
5. **Web** (`web.py`): `GET /api/spylead` (configured, snapshot time, and per series its name, in-sample mark and the first and last day with a value), `POST /api/spylead/snapshot` (202 with the job; 409 JOB_RUNNING; 409 SPYLEAD_NOT_CONFIGURED when the URL is empty), and `app.services.spylead` in the state document (`configured`, `snapshot_at`, `series`). `__main__.py` hands `config.spylead` to the jobs.
6. **UI**: in the Data view, the spylead snapshot (time, series, in-sample marks) with [Snapshot now]; on Home, spylead's status; in the rule builder's operand picker, the stored series, the in-sample ones marked; on a result and on an optimization, the IN_SAMPLE_SERIES banner names the series. New strings in both locales.
7. **Tests** (AC-23), with an aiohttp test server that mimics spylead's API on 127.0.0.1 (`tests/fakes.py`):
   - the client's calls in order (login, state, SPY, the KEPT series only, F only when OK), the cookie on every call after login, the translation (null → NaN), MISSING_KEY without a request, a refused token, a bad payload, and no token in any error;
   - the snapshot job replaces the stored set in one transaction; an unreachable spylead keeps the previous snapshot and records SPYLEAD_UNREACHABLE; an update runs SNAPSHOT when spylead is configured and not otherwise;
   - alignment by calendar day on the daily and the minute timelines, NaN outside, and a truncation test;
   - IN_SAMPLE_SERIES through a rule and through `bars.ext()`, on a backtest and on an optimization, and not for SPYL1;
   - the routes, the state document and the registry with stored series.
8. Run `.venv/bin/python -m pytest -q` and smoke-test the Data view and the rule builder in headless Chromium.

### Readings where the spec is silent

- **spylead's payloads** are read as: `GET /api/state` → `{"series": [{"name", "status"}, …], "f": {"status"}, …}`; each chart's `rt`, `f` and `composite` → `{"first_day", "values"}`, where `first_day` is a `YYYY-MM-DD` date or a day number and `values` holds numbers or `null`. Login answers 2xx and sets the session cookie. The owner checks these against spylead's API documentation (checklist below); a mismatch shows as SPYLEAD_UNREACHABLE with BAD_PAYLOAD and the field it missed.
- **Any failure of a snapshot** (unreachable, refused, bad payload, missing token) keeps the previous snapshot and records SPYLEAD_UNREACHABLE with the error, since in each case spylead gave no usable snapshot.
- **The flag's detail** is the list of in-sample series the run read (`["F"]`), so the banner can name them.

## 2. Extension points

- **Another spylead series** is one more entry in the client's translation; the registry, the rule builder and `bars.ext()` take any stored name.
- **ibguard** (prompt 23) reuses the requester's POST and cookie support.

## 3. Refactor seams

- The client returns plain `ExternalSeries`; the job owns storing them, so another source of external series would only need a client.
- Alignment lives in `View.series`, the one place a strategy reads an external series.

## 4. Deferred items (appended to TODO.md)

- None.

## Manual checklist (on the PC)

1. With spylead running, check its API documentation against the payloads assumed above (login, state's `series` and `f.status`, the `{first_day, values}` objects).
2. Data view → [Snapshot now]: the snapshot time updates and lists SPYL1, the LEADL1_ series spylead keeps, and F and COMPOSITE marked in-sample (when spylead's F is OK).
3. Rule builder: an operand of kind "External series" offers the stored series; build a rule on F, run it: the result shows the banner "In-sample series: optimistic" naming F.
4. Stop spylead and run [Snapshot now]: the previous snapshot stays, and Home shows an event SPYLEAD_UNREACHABLE.
