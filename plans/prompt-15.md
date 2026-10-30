# Prompt 15: Web API core and wiring

Covers SPEC.md §19 (the routes of this prompt), §21, §22 and §23. Acceptance: AC-19, AC-20 for these routes, and the runtime part of AC-28.

## 1. Steps

1. **`btlab/web.py`**, an aiohttp application over a `Services` value (config, stores, jobs, the plugin folder, the clock and the sleep). It validates input and calls the jobs and the store; it holds no backtest logic.
   - **Session (§19).** The cookie `btlab_session` holds the hex HMAC-SHA256 of `btlab-session` keyed with `web.token`: `HttpOnly`, `SameSite=Strict`, `Max-Age` 30 days, `Secure` under `X-Forwarded-Proto: https`. Tokens and cookies compare in constant time. Every `/api/*` route but login needs it (401 UNAUTHORIZED).
   - **Bodies.** POST, PUT and PATCH take `application/json` (415 UNSUPPORTED_MEDIA_TYPE); JSON that does not parse is 400 INVALID_JSON; a non-finite number anywhere (`NaN`, `Infinity`, `1e999`) is 400 INVALID_FIELD at its path; a boolean in a number field is INVALID_FIELD from the field checks.
   - **Errors.** `{"error", "message", "path"?}`; RULE_INVALID adds `errors`. An unknown id or `/api` path is 404 NOT_FOUND, a wrong method 405 METHOD_NOT_ALLOWED. An unexpected exception is logged with its traceback, recorded as INTERNAL_ERROR and answered 500 INTERNAL_ERROR; the process goes on (§23).
   - **Headers (§21)** on every response, errors and static files included, through `on_response_prepare`: the CSP, `nosniff`, `no-referrer`, and `Cache-Control: no-store` on `/api/*`, `no-cache` elsewhere.
   - **Static files** from `btlab/static/` without a session: `/` is `index.html` (a minimal placeholder until prompt 17); a path that leaves the folder is 404.
   - **Listening** on 127.0.0.1 only, at `web.port` (`start_site`).
   - **Routes:**
     - `POST /api/login` (204 and the cookie; 401 LOGIN_FAILED after the injected 1 s sleep), `POST /api/logout` (204, cookie cleared);
     - `GET /api/state`: `app` (server time, mode, which keys are set, the limits in trade mode, the schedule with each market's next update), `markets` (data_start, last day, symbols, last update), `jobs` (each lane's progress or null) and `events` (the latest 50, newest first). `services` and `live` arrive with prompts 20 to 23;
     - lists: `GET`, `POST` (201 `{"id"}`; 409 ALREADY_EXISTS), `PUT /{id}` (name and symbols, normalized by §7's rules, 400 INVALID_FIELD at `symbols[i]`; a new symbol joins the market's symbols), `DELETE /{id}`;
     - symbols: `GET ?market=` (name, category, exchange, data range, last error, minute flag, minute coverage by status), `PATCH /{market}/{symbol}` (category, minute flag, US borrow fee; SETTING_CHANGED events), `DELETE /{market}/{symbol}` (purge, minute bars first; 409 IN_USE while in a list);
     - `POST /api/update` (202; 409 JOB_RUNNING), `POST /api/jobs/{lane}/cancel` (204; 404 for an unknown lane);
     - `GET /api/strategies` (built-ins with their schema per market, plugins with timeframe, style, output and schema, and load errors) and `POST /api/strategies/reload`;
     - `GET /api/indicators` (the registry of §8.6);
     - rules: `GET`, `POST` (201), `PUT /{id}`, `DELETE /{id}` (400 RULE_INVALID with the errors of §9.6; 409 ALREADY_EXISTS), `POST /api/rules/validate` (200 `{"ok", "errors"}`).
2. **`btlab/__main__.py`** (§23): config and tax table, then the process lock on `data/btlab.lock` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere; a second process exits before opening a database or the port), both stores, the plugins, the jobs with the real pool and the notifier, the web server, the scheduler (a tick at once for the catch-up, then every minute), ready. Ctrl+C stops accepting requests, interrupts the jobs (their pools close with them), and closes the stores; the lock goes with the process.
3. Small additions: `Store.market_last_day`, `scheduler.next_time`, and `plugins.PluginFolder` (the loaded plugins and errors of `strategies/`, reloaded on demand, read by the jobs and the routes).
4. **Tests** (`tests/test_web.py`, `tests/test_runtime.py`), with aiohttp's test utilities, a stub jobs object and temporary stores:
   - AC-19: every `/api/*` route but login refuses a request without the session; 415, INVALID_JSON, non-finite and boolean numbers; the §21 headers on an API answer, an error and a static file; 127.0.0.1 only; the cookie's flags, with `Secure` behind `X-Forwarded-Proto: https`; no configured secret in any captured log line, event, answer or error;
   - login and its 1 s delay, logout;
   - each route's success and error paths, and 409 JOB_RUNNING from a busy lane;
   - the state document's shape;
   - AC-28: a second process exits while the lock is held, a killed holder does not block a restart, and the startup and shutdown order.
5. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **§23's order wins over the prompt's "lock first":** the lock file lives in `data/` next to `config.toml`, so the config comes first; nothing opens a database or the port before the lock.
- **Rounding to 6 significant digits** applies to the data payloads of prompt 16 (series, candles, metrics), not to configurations, rules or settings, which must come back as stored.
- **Cancelling an idle lane** answers 204; NOT_CANCELLABLE arrives with the LIVE lane (prompt 22).
- **A symbol added to a list** joins the market's symbols at once, with its category by rule, so the Data view shows it before the next update fetches it.
- **POST /api/update** answers 202 with the lane and kind of the job.

## 2. Extension points

- **A route** is one handler and one line of the route table.
- **A part of the state document** is one function of the services.

## 3. Refactor seams

- The web layer reaches the system only through `Services`; tests give it stubs.
- Validation helpers raise one `ApiError`, which the middleware turns into the error format.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-19 holds, the routes of this prompt meet AC-20, and AC-28's runtime part holds.** `.venv/bin/python -m pytest -q`: 1077 passed (also with `-X dev -W error`). The app was not started; the startup and shutdown order is tested through `run()` on an ephemeral 127.0.0.1 port.
- Built as planned. `JobRunning` from any route becomes 409 JOB_RUNNING in the error middleware, so prompt 16's routes get it for free.
- The CLI test that expected the placeholder exit now checks that a second process exits with code 3 before creating a database.
- **For prompt 16:** add routes to the table in `make_app`; read bodies with `read_json`, check fields with `_object`/`_text`, raise `ApiError`; `Services.jobs` is the `Jobs` of prompt 14. Rounding to 6 significant digits belongs to the series, candle and metric payloads only.
