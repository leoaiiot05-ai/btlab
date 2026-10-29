# Prompt 1: Scaffold, lock file, config and tax table

Covers SPEC.md §2, §4.1, the tax table of §10.6, §21, §22 and the logging line of §23. Acceptance: AC-1.

## 1. Steps

1. **Dependencies (§21).**
   - `requirements.in`: exactly `numpy`, `aiohttp`, `tzdata`, `ib_async==2.1.0`, `pytest`.
   - `python -m venv .venv`, `.venv/Scripts/python -m pip install --upgrade pip`,
     `.venv/Scripts/python -m pip lock -r requirements.in -o pylock.toml`, `.venv/Scripts/python -m pip install -r pylock.toml`.
   - No `pyproject.toml`; tests and the app run from the repository root.
2. **`.gitignore`**: `config.toml`, `data/`, `logs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.claude-runs/`, `.claude/settings.local.json`. Never `buildctl/` or `.claude/settings.json`. The entries are unanchored, as listed, so no committed folder may be named `data/` or `logs/` (test fixtures included).
3. **`btlab/__init__.py`**: a docstring only.
4. **`btlab/markets.py`** (pure; imports only `dataclasses` and `datetime`). Frozen dataclasses:
   - `Session(open, close)` in local time;
   - `PriceLimit(start, pct)`: TW ±7% from `date.min`, ±10% from 2015-06-01; US none;
   - `PerShareCommission(per_share, min, max_pct)` (US) and `RateCommission(rate_pct, min)` (TW);
   - `Market`: code, currency, time zone name, session, categories, benchmark, initial capital, lot, board lot (TW 1,000; US none), price limits, commission, transaction tax (TW only), slippage bps, short collateral m, borrow fee % (US), short-sale fee % (TW), dividend withholding %, automatic-update weekdays (the time is `[schedule]` in config.toml), live order kinds.
   - `US`, `TW`, `MARKETS = {"US": US, "TW": TW}`, and `DATA_START_YEARS = 31` (§4.1).
5. **`taxes.toml`** verbatim from §10.6, and **`btlab/taxes.py`** (pure after loading):
   - `load_taxes(path)` reads the file (UTF-8, BOM tolerated) and calls `parse_taxes(doc)`, which validates and builds a `TaxTable` of `TaxEntry(label, category, rate_pct, first, last)`.
   - Rules: only `[[tw]]` at the top level; per entry only `category`, `rate_pct`, `from`, `to`; category one of STOCK, STOCK_DAYTRADE, ETF, TDR, BOND_ETF; `rate_pct` a finite number from 0 to 100; dates are `"YYYY-MM-DD"` strings, `from` defaults to 1900-01-01 and `to` to 9999-12-31, `from ≤ to`; ranges of one category must not overlap; STOCK, ETF, TDR and BOND_ETF must cover 1900-01-01 to 9999-12-31.
   - All problems are collected into one `TaxTableError`. Messages name the entries (`[[tw]] entry 5`, with category and dates); the tax table holds no secrets.
   - `TaxTable.rate_pct(market, category, day, day_trade=False)`: `day` is days since 1970-01-01 (§3). US gives 0. TW: a day trade in a STOCK takes STOCK_DAYTRADE when one is in force that day, otherwise STOCK; other categories ignore the flag. A category the market does not have raises `ValueError`.
6. **`config.example.toml`** verbatim from §22 (mode "test", `[limits]` commented out), and **`btlab/config.py`**:
   - `load_config(path) -> Config`: frozen dataclasses per section (`WebConfig`, `TiingoConfig`, `FinMindConfig`, `FredConfig`, `ServiceConfig` for spylead and ibguard, `IbConfig`, `MinuteConfig`, `OrdersConfig`, `Limits`, `ScheduleConfig`, `EmailConfig`). `Config.path` is the absolute path of the file; `Config.root` is its folder, where `taxes.toml`, `strategies/`, `data/` and `logs/` live (§22).
   - One validator per key. Every problem is collected into one `ConfigError` whose messages name the key (`web.token`) and never quote a value: wrong types name only the expected and the found TOML type, a TOML syntax error gives only its line and column, and exceptions are raised `from None` so no chained message leaks.
   - Rules: unknown keys, missing keys (a missing section is named as such), wrong types (booleans are not numbers; integers are accepted where a number is expected; NaN and infinities are rejected); `mode` "test" or "trade"; `web.token` not the placeholder and at least 16 characters; `ib.client_id` not 17; `[limits]` required in trade mode with all five keys above 0 (counts are integers), each missing key named; when present in test mode it is validated the same way.
   - Sanity rules not spelled out in §22, reported the same way: ports 1 to 65535; `minute.backfill_years` above 0; limit offsets from 0 to below 100; `us_order_kind` "MOO" or "LIMIT"; `[schedule]` times `"HH:MM"`; `email.language` "zh-TW" or "en".
   - `[email]` is a required section: its "optional" comment means email is off when `smtp_host` is empty.
   - Secret fields (web token, API keys and tokens, SMTP password) are `repr=False`, so printing a `Config` shows no secret.
   - `startup_warnings(config)`: one line per empty key or token of an enabled service (Tiingo, FinMind, FRED always; spylead and ibguard only when their URL is set), saying its requests will fail with MISSING_KEY.
7. **`btlab/__main__.py`**: `main(argv)`: argparse (`--config`, required) → `load_config` → `load_taxes(root / "taxes.toml")`; on `ConfigError` or `TaxTableError` print the message to stderr and return 2, before any logging or file is created. Then `setup_logging(root / "logs")` (§23: root logger DEBUG; console handler at INFO when a console exists, since `pythonw.exe` has none; `RotatingFileHandler` `logs/btlab.log` at DEBUG, 5 MB, 5 backups, UTF-8; UTC timestamps), log the startup warnings, log that the app is not implemented yet, and return 1.
8. **Tests** (no network, no IB):
   - `tests/test_config.py`: a valid file (types converted, limits `None` in test mode, valid limits in trade mode); a missing file; an unknown key; a missing key and a missing section; wrong types; the placeholder token; a short token; `client_id = 17`; trade mode with `[limits]` absent, a key missing, zero and negative values; empty keys accepted with warnings; `config.example.toml` fails only on the placeholder; secrets absent from `repr(config)`; and a parametrized AC-1 check that no error message contains any value of the file (sentinel values everywhere, including the offending one, and a TOML syntax error inside a secret).
   - `tests/test_taxes.py`: the committed file loads; the lookups of AC-1; categories without a day-trade rate; US gives 0; overlaps, a gap (inside, before the first and after the last range), a category with no entry, an unknown category, an unknown key, a bad rate, a bad date and `from` after `to` are rejected, naming the entries.
   - `tests/test_markets.py`: the §4.1 values; the dataclasses are frozen; both time zones resolve through `zoneinfo` (needs `tzdata` on Windows).
   - `tests/test_cli.py`: `python -m btlab --config config.example.toml` in a subprocess, with a `sitecustomize.py` audit hook that kills the process on any `socket.*` event, exits 2 naming `web.token` as the placeholder; `main()` in-process on a valid temporary config sets up logging, logs the warnings and "not implemented", returns 1, and writes no secret to the log; the logging handlers have the §23 levels and rotation.
   - `tests/test_repo.py`: `requirements.in` lists exactly the five requirements; every package in `pylock.toml` has a SHA-256 for each artifact, and the five requirements are locked (`ib_async` at 2.1.0); `.gitignore` holds the required entries and does not ignore `buildctl/` or `.claude/settings.json`.
9. Run `.venv/Scripts/python -m pytest -q` until it passes.

## 2. Extension points

- `Market`: one more constant is one field; a market is one more instance in `MARKETS`.
- Tax table: entries are data in `taxes.toml`. A tax table for another market is one more top-level array (for example `[[us]]`) accepted by `parse_taxes`; the lookup already takes the market.
- Config: a key is one validator in `_SECTIONS` plus one dataclass field; a section is one dataclass plus one `_SECTIONS` row.
- `startup_warnings` is the one place for configuration warnings (prompt 14 can add the empty-`smtp_host` warning of §17.6 there or in the notifier).
- `__main__.main` is where prompt 15 wires the process lock, stores, plugins, web server and scheduler, in the order of §23.

## 3. Refactor seams

- Reading and validating are separate: `parse_taxes(doc)` is pure; `load_config` reads through `_read_toml` and validates the parsed document.
- Day numbers (days since 1970-01-01, §3) are converted privately in `taxes.py`; `calendar.py` (prompt 4) can own the public conversion, and `taxes.py` can then import it.
- `setup_logging` in `__main__.py` is where prompt 3's redaction helper can be attached as a logging filter.
- Exit codes: 2 for an unusable config or tax table (as argparse uses for bad arguments), 1 for "not implemented yet". Prompt 15 replaces the latter with the running app.

## 4. Deferred items (appended to TODO.md)

- Reload `config.toml` and `taxes.toml` without a restart (Operations). Seam: `load_config` and `load_taxes`, called from `__main__.py`.
- Format checks for URLs and email addresses in `config.toml` (Operations). Seam: the per-key validators in `config.py`.

## Result

- **Lock.** Python 3.13.7, pip 26.2.2. `pylock.toml` (lock-version 1.0, created by pip) pins 20 packages. Each has one wheel for win_amd64 / CPython 3.13, and every wheel has a SHA-256. The suite checks this (`tests/test_repo.py`). The venv was installed with `pip install -r pylock.toml`.
- **tzdata is 2025.3, not the latest.** `ib_async` 2.1.0 itself requires `tzdata>=2025.2,<2026.0`. For America/New_York and Asia/Taipei that is current enough.
- **AC-1 holds.** `.venv/Scripts/python -m pytest -q`: 130 passed. The suite also passes with `-W error`.
- `taxes.toml` and `config.example.toml` match their SPEC.md blocks byte for byte (checked once, by script).
- **The network guard works.** The `sitecustomize.py` guard of the subprocess test was checked against a child that calls `socket.getaddrinfo`: it ends the child with exit 97.
- **For later prompts:**
  - `tests/test_cli.py` calls `main()` in-process on a valid config, because today `main()` only validates and exits. Prompt 15 must replace that test when `main()` starts the app. The subprocess test stays valid, because the config is still loaded first (§23).
  - `.gitignore` ignores `data/` and `logs/` at any depth, so test fixtures must not live in folders with those names.
- **Two shell commands were refused by the permission checker.** One used `${PIPESTATUS[0]}`; the other was a heredoc holding a Python dict literal. Each was rerun without that construct, and nothing was blocked.
