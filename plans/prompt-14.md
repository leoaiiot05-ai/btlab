# Prompt 14: Jobs and scheduling

Covers SPEC.md §5 (budgets and windows), §17 (without §17.3, prompt 21, and the live follow-ups, prompt 22) and §23 (keep-awake). Acceptance: AC-18 for these parts, and the notifier part of AC-28.

## 1. Steps

1. **`btlab/jobs.py`** composes the clients, prices.py, the store, backtest.py and optimize.py; it holds no logic of theirs.
   - **Lanes (§17.1).** `Jobs` runs one job per lane: DATA (UPDATE_US, UPDATE_TW) and COMPUTE (BACKTEST, OPTIMIZE) now; MINUTE and LIVE come with prompts 21 and 22. Starting a job on a busy lane raises `JobRunning` (the API's 409 JOB_RUNNING). Each job has its progress (kind, phase, done, total, current item, start time, message) for the state document.
   - **Stopping.** `cancel(lane)` and, at shutdown, `interrupt()` stop jobs cooperatively: a job checks between steps, and its sleeps and pool waits end at once (so a job waiting for Tiingo's budget stays interruptible). The job ends CANCELLED or INTERRUPTED; what it stored stays, and an optimization saves its partial results.
   - **The record.** Every job's start and end go to `job_runs` with its outcome and message, and its end is an event: UPDATE_FINISHED (UPDATED, PARTIAL, MAINTENANCE), UP_TO_DATE, BACKTEST_SAVED, OPTIMIZATION_SAVED, JOB_FAILED, JOB_CANCELLED or JOB_INTERRUPTED; an update also records UPDATE_STARTED. An unexpected exception is logged with its traceback, recorded as INTERNAL_ERROR (and emailed once per run of the app) and fails the job; it never stops the process (§23).
   - **UPDATE_US and UPDATE_TW (§17.2, §5).** One aiohttp session per job (the factory is injected), the clients over `Requester`s with the injected clock and the job's sleep, and the Tiingo and FinMind budgets loaded from and saved to the store after each request. "Today" is the market's local date. The first update fixes `data_start` (today minus 31 years). The symbols are the market's benchmark first, then every list's symbols; a new one is added with its category by rule and fetched from `data_start`.
     - US: BARS (one Tiingo request per symbol gives bars and actions: from 7 days before the latest stored day, upserted; the actions normalized by `normalize_us_actions` and replaced over the request's window), then RATES.
     - TW: CALENDAR (`TaiwanStockTradingDate`), INFO (`TaiwanStockInfo` at most once a day: names and exchanges of the market's symbols), the market's split and par-value tables, then per symbol BARS (prices) and ACTIONS (dividend results, policies, capital reductions, short suspensions from 400 days before the latest stored day, normalized by `normalize_tw_actions` and replaced over that window), then RATES.
     - RATES: DTB3 from 30 days before the latest stored rate.
     - A symbol that fails stores `last_fetch_error` and an event SYMBOL_FAILED, and the job goes on; a success clears the error. Action issues become BAD_ACTION or DIVIDEND_SPLIT_UNKNOWN events.
     - Outcomes: FAILED without a request when the market's key or token is empty (MISSING_KEY), or when every symbol failed; MAINTENANCE when FinMind's window stops it; PARTIAL when some symbols or the rates failed; UP_TO_DATE when nothing was added, changed or removed; else UPDATED, with the counts in the message.
     - When the hourly budget is spent the job's message says until when (Asia/Taipei). Tiingo's distinct symbols of the month are kept in `settings`; passing 450 records TIINGO_SYMBOL_BUDGET.
     - The SNAPSHOT phase arrives with prompt 20, the follow-ups with prompts 21 and 22.
   - **BACKTEST (§17.5).** Validation first (`RunConfigError` for the API's 400), then LOAD (`load_run`, in a thread), CHECK (the look-ahead check of a vectorized plugin), SIMULATE (`run_backtest` in a worker of the injected pool; it computes the metrics too) and SAVE (`save_result`).
   - **OPTIMIZE (§17.5).** `parse_request` first, then LOAD and `optimize()` in a thread (ESTIMATE, GRID, OOS or WFO), then SAVE. `job.ready` resolves once the estimate is known: None when the runs go on, or the `NeedsConfirmation` with which the job ends (outcome NEEDS_CONFIRMATION), for the API's 409.
   - **Keep-awake (§23).** `KeepAwake` holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while any job runs and clears it after the last; elsewhere than Windows it does nothing.
   - **The schedule.** `Jobs.tick()` starts the updates the scheduler finds due, one at a time while the DATA lane is free; when a scheduled update ends FAILED for the fourth time (its third retry), it emails UPDATE_FAILED.
2. **`btlab/scheduler.py`** (pure): the latest due time of each schedule in Asia/Taipei (US Tuesday to Saturday, TW Monday to Friday, at the configured times); an update is due when no run started after it (so a PC that was off runs one catch-up, not one per missed day); a scheduled run that ended FAILED is due again 30 minutes after it ended, at most 3 times; one that ended MAINTENANCE at the next 07:05 Asia/Taipei. `exhausted(...)` tells when the retries are spent.
3. **`btlab/notifier.py`**: the `Notifier` protocol; `make_notifier(email, mode)` gives an `SmtpNotifier` (smtplib through `asyncio.to_thread`, implicit TLS on port 465, STARTTLS otherwise, login when a username is set) or, with an empty `smtp_host`, `Silent` and a startup warning. Subjects start with `[btlab]`, `[btlab][TRADE]` in trade mode; texts in zh-TW or en. A failed send is logged by its type, never raised. UPDATE_FAILED and INTERNAL_ERROR now; the other emails with their features.
4. **`btlab/store.py`**: one lock around the connection, opened for use from any thread, because jobs load and save in worker threads to keep the event loop free; `setting(key)` and `save_setting(key, value)` for the new settings (the stock-info day, Tiingo's symbols of the month).
5. **Tests** (`tests/test_jobs.py`, `tests/test_scheduler.py`, `tests/test_notifier.py`), with the HTTP fakes as sources, temporary stores, the injected clock and sleep, and the in-process pool:
   - one job per lane and `JobRunning`; DATA and COMPUTE side by side; progress; cancel and interruption, with the data stored so far kept; keep-awake held while a job runs;
   - US: UPDATED with the 7-day overlap upserting a revised row, then UP_TO_DATE with nothing stored; PARTIAL with SYMBOL_UNKNOWN on its row; FAILED on MISSING_KEY without a request; the wait for Tiingo's budget with its message, the budget persisted; TIINGO_SYMBOL_BUDGET;
   - TW: an update through every phase with normalized actions and suspensions stored; INFO once a day; MAINTENANCE;
   - the scheduler: due times on the right weekdays in Asia/Taipei, one catch-up after downtime, retries after 30 minutes and at most 3, the MAINTENANCE retry at 07:05, nothing while a run is going on or after a manual run answered the due time;
   - `tick` starting a due update, and UPDATE_FAILED emailed after the third retry;
   - a backtest job and an optimization job saved through the store; NEEDS_CONFIRMATION; a cancelled optimization saved with its partial results;
   - the notifier: subjects, TLS by port, no exception on SMTP failure, off with an empty host.
6. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **Every symbol failed** is FAILED rather than PARTIAL ("some symbols failed").
- **DTB3 failing** makes an update PARTIAL, with an event SYMBOL_FAILED for DTB3: the bars are still good.
- **Action issues** are recorded for actions on days after the symbol's latest stored bar (all on a first fetch), so the 400-day window does not repeat the same events every day.
- **An action or suspension outside the request's window** (announced ahead) is left for a later update, which will fetch it inside its window.
- **TIINGO_SYMBOL_BUDGET** is recorded when an update takes the month's distinct symbols past 450, once a month.
- **A manual update after a due time** answers it; only scheduled runs are retried.
- **NEEDS_CONFIRMATION** ends the job without runs; its event is JOB_CANCELLED with the estimate, §18 having no other kind for it.
- **The METRICS phase** runs inside the worker with the simulation, so the job shows SIMULATE until the result returns.

## 2. Extension points

- **A job kind** is a lane entry and a work function; MINUTE_BACKFILL, SPYLEAD_SNAPSHOT and the live jobs plug in the same way.
- **An update phase** is one step of the market's update; SNAPSHOT and the follow-ups come after RATES.
- **An email** is one entry of the notifier's texts.

## 3. Refactor seams

- The scheduler decides from the clock, the schedule and the job runs; `Jobs.tick` only starts what it returns.
- Clients, budgets, sessions, the clock, the sleep, the pool and the notifier are injected.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-18 holds for the parts of this prompt** (the follow-ups and LIVE_DATA_INCOMPLETE arrive with prompts 21 and 22), and the notifier part of AC-28. `.venv/bin/python -m pytest -q`: 1051 passed (also with `-X dev -W error`). `scheduler` joined the pure modules.
- Built as planned. Stopping is cooperative for every job: `Job.check()` between steps, and `Job.race()` for sleeps, thread work and pool waits, so a stop before a job's first step is recorded too.
- The store now serializes its connection with a lock and allows any thread; `setting` and `save_setting` replaced the private reader.
- The optimizer's estimate uses the jobs' clock, so tests drive it.
- **For prompt 15:** build `Jobs(store, minute_store, Keys(...), taxes, plugins, process_pool(default_workers()), default_workers(), notifier=make_notifier(config.email, config.mode))`, call `jobs.tick(config.schedule)` every minute from the event loop, `jobs.running()` feeds the state document's jobs, and `await jobs.interrupt()` at shutdown. **For prompt 16:** `jobs.backtest(raw)` and `jobs.optimization(raw)` raise `RunConfigError` or `OptimizeError` before any job; `await job.ready` gives the optimization's `NeedsConfirmation` (409) or None (202); `jobs.cancel(lane)`; `JobRunning` is 409.
