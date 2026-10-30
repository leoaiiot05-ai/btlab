# Prompt 24: Operations and README

Covers SPEC.md §23 (runtime), §24.3 (bench and checks on the PC), §25 (limitations, in the README), §27 (owner setup) and the `scripts/` and `run.bat` rows of §2. Acceptance: the runtime part of AC-28 (the autostart script, and the lock, startup, shutdown and logging tests that already exist).

## 1. Steps

1. **`scripts/install-autostart.ps1`**, for Windows PowerShell 5.1 (no `&&`, `||`, `??`, `?.` or ternaries; ASCII only, because 5.1 reads a file without a BOM in the ANSI code page):
   - the project folder is the script's parent folder; the script refuses when `.venv\Scripts\pythonw.exe` or `config.toml` is missing there, naming the install step to run;
   - it writes the launcher `%LOCALAPPDATA%\btlab\launch.ps1` (UTF-8 with BOM, which 5.1 reads correctly) with the project path filled in. The launcher waits up to 10 minutes for the project folder (polling every 5 s), then starts `.venv\Scripts\pythonw.exe -m btlab --config config.toml` in that folder, and appends one line to `%LOCALAPPDATA%\btlab\launch.log`: started, the folder never appeared, the venv is missing, or btlab exited within 15 s with its exit code (pythonw has no console, so a config error would otherwise vanish);
   - it registers the per-user logon task `btlab` (trigger at the user's logon, interactive principal at limited rights, no time limit, runs on battery, one instance) whose action runs the launcher hidden with `-ExecutionPolicy Bypass`; re-running the script replaces the task and removes a Startup shortcut left by an earlier fallback;
   - if registering the task is refused, it puts a shortcut `btlab.lnk` that runs the launcher in the user's Startup folder instead, and says so (a `.ps1` file placed there would open in Notepad, so the Startup folder gets a shortcut to the launcher);
   - `-Remove` deletes the task, the Startup shortcut and `%LOCALAPPDATA%\btlab`, and says what it removed.
2. **`run.bat`**: from its own folder, refuses with a hint when `.venv\Scripts\python.exe` is missing, otherwise runs `.venv\Scripts\python.exe -m btlab --config config.toml` in the console; on a non-zero exit (config error 2, lock held 3) it pauses so a double-clicked window stays readable. No labels or `goto`, so it runs the same with LF or CRLF line endings.
3. **§23 check**, item by item:
   - startup order, the lock (exits before any database or the port; a killed holder does not block), logging (console INFO, file DEBUG, 5 MB × 5; no console handler under pythonw), shutdown order: as specified, with tests from prompts 15 and 23;
   - **gap, keep-awake**: it holds only while a job runs. §23 also wants it while a btlab order works. Fix: `Jobs` keeps one extra hold while any line is SENT or WORKING (trade mode only; test mode never has such lines), checked at every scheduler tick and as each job ends, on the same `KeepAwake` counter, so the hold carries over from the LIVE_SEND job to the working orders without a gap;
   - **gap, unexpected exceptions**: an exception while applying an IB order report is caught by ib_async's event library, which logs it with its traceback, but it is not recorded as INTERNAL_ERROR and the batch does not stop (§15.9, §23). Fix: `Trader.on_report` catches it, logs the traceback, stops the line's batch when the line is known, records INTERNAL_ERROR and sends the INTERNAL_ERROR email (once per run, as the notifier already enforces);
   - **gap, logging under pythonw**: an unexpected exception at startup (say the port is taken) escaped `main()` as a traceback on stderr, which `pythonw.exe` does not have, so the autostart lost it. Fix: `main()` logs it with its traceback to `logs/btlab.log` and exits with 1, still releasing the lock;
   - a process killed without Ctrl+C (Task Manager, sign-out under the autostart) leaves its running job's row without an end; the scheduler treats it as a run that started after the due time, which is what §17.4 says for an interrupted run, and the UI shows the last finished run. No change.
4. **Gap found while writing the README, §20.2 item 5**: a result page had no "use for a live portfolio" and no "write to ibguard" action. Fix: both buttons (the second only for a daily run with the ibguard rule while ibguard is configured) open the Live view with that backtest chosen in the create form and the ibguard panel, once, as "compare with" already hands its choice to the results list; two locale keys in both languages, and the worker's shell version.
5. **`README.md`** for the owner, in English: what btlab does and does not do; prerequisites; keys and tokens; install from `pylock.toml`; `config.toml` key by key; first run in Edge and installing it as an app; lists, the first update and its duration (Tiingo's 45 requests an hour, so about 45 new US symbols an hour; FinMind about 5 requests per symbol within 550 an hour), the schedule and catch-up; minute bars and backfill; backtests (price modes, fill timing, order of events, sizing, exits and intrabar paths, costs and taxes, shorting, every flag, metrics, CSV); built-ins; plugins in both styles with the two examples, and the look-ahead check; the rule builder; optimization and its traps; live portfolios in test mode step by step; trade mode, limits, ibguard coordination and threshold writing; iPhone and iPad through Tailscale Serve on port 10000, the home-screen icon, offline viewing, and why never Funnel or a public port; IB Gateway settings and Taiwan; notifications; autostart, run.bat, stopping btlab, the drive letter, exFAT and `safe.directory`, backups; the bench and the checks of §24.3; the limitations of §25 in plain words; troubleshooting by error code.
6. **Tests**:
   - `tests/test_autostart.py`: the script exists and is ASCII; it has a `-Remove` switch whose path unregisters the task, deletes the shortcut and the launcher folder; it names the task `btlab`; it writes the launcher under `$env:LOCALAPPDATA\btlab`; the launcher waits 10 minutes for the project folder and starts `pythonw.exe -m btlab --config config.toml` in it; registration failure falls back to the Startup folder and says so; no syntax that Windows PowerShell 5.1 lacks; on a PC with `powershell.exe`, PowerShell's own parser reads the script without errors (parsing does not run it); `run.bat` starts the venv's python with `config.toml` from its own folder;
   - keep-awake: in trade mode the hold stays after the LIVE_SEND job while the order works, and ends at the tick after the fill;
   - a failing report is recorded as INTERNAL_ERROR, stops the batch, and is not raised into the event source;
   - an unexpected error at startup reaches `logs/btlab.log` with its traceback, exits with 1 and releases the lock;
   - AC-28: the existing lock, startup, shutdown, logging and notifier tests still pass.

### Readings where the spec is silent

- "Puts the launcher in the user's Startup folder" is read as a shortcut there that runs `%LOCALAPPDATA%\btlab\launch.ps1`, because Windows opens a `.ps1` file in the Startup folder with Notepad.
- The launcher's log `%LOCALAPPDATA%\btlab\launch.log` and its early-exit check are additions for diagnosis; they write nothing secret (paths and exit codes only).
- The task runs only while the owner is signed in (interactive logon, no stored password), as "per-user logon task" says.

## 2. Extension points

- **Another way to start**: the launcher is independent of how it is started; a Windows service (TODO.md) would replace only the registration part of the script.
- **README sections** follow SPEC.md's order, so a new feature adds a section next to its neighbours.

## 3. Refactor seams

- Keep-awake stays one counter (`KeepAwake` in `jobs.py`) for jobs and working orders, because Windows keeps one execution state per thread: a second counter would clear the first one's request.
- The install script separates the launcher (wait and start) from its registration (task, or Startup shortcut).

## 4. Deferred items (appended to TODO.md)

- A tray icon or a page button to stop the autostarted btlab gracefully; today Ctrl+C works only in `run.bat`'s console, and the autostarted process is ended from Task Manager (safe, but without the orderly shutdown of §23).
- Wake the PC from sleep for a scheduled update (a wake timer); today a sleeping PC runs the missed update when it wakes.

## Manual checklist (SPEC.md §24.3 item 8, on the PC)

1. In PowerShell (Windows PowerShell 5.1 is fine), from the project folder: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1`. It says the task `btlab` is registered (or that it used the Startup folder, and why).
2. Check `%LOCALAPPDATA%\btlab\launch.ps1` exists and names `E:\00_CODING\btlab`; Task Scheduler shows the task `btlab` under the Task Scheduler Library, trigger "At log on of <you>".
3. Stop btlab if it runs (Ctrl+C in `run.bat`'s window). Sign out and sign in. Within a minute of the drive mounting, `http://127.0.0.1:8091` answers, and `%LOCALAPPDATA%\btlab\launch.log` ends with "btlab started".
4. With the drive disconnected, sign out and in: after 10 minutes `launch.log` says the folder did not appear; reconnect, and start btlab with `run.bat`.
5. Start a second copy with `run.bat` while the autostarted one runs: it exits with code 3 ("another btlab process is running") and the window pauses.
6. Rename `config.toml` briefly and sign out and in: `launch.log` says btlab exited at once with code 2; restore the name.
7. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove` removes the task, any Startup shortcut and `%LOCALAPPDATA%\btlab`; install again afterwards if you want autostart.
8. Keep-awake: while an update runs (or, in trade mode, while an order works), `powercfg /requests` lists `pythonw.exe` (or `python.exe`) under SYSTEM; after the job ends it is gone.
9. Read README.md from top to bottom on the PC and follow it on a clean PC or a fresh folder: install, configure, first update, a first backtest and a test-mode live portfolio, without other help.
10. On a result page of a daily backtest: **Use for a live portfolio** opens Live with that backtest chosen; with ibguard configured and a run that used the ibguard rule, **Write to ibguard…** opens the ibguard panel with it chosen.

## Result

- `scripts/install-autostart.ps1` (Windows PowerShell 5.1, ASCII): the logon task `btlab`, the launcher `%LOCALAPPDATA%\btlab\launch.ps1` that waits 10 minutes for the project folder and starts `pythonw.exe -m btlab --config config.toml` there with a line in `launch.log`, the Startup-folder shortcut when the task is refused, and `-Remove`. `run.bat` starts btlab in a console and pauses on a non-zero exit.
- §23 gaps fixed: keep-awake now also holds while a btlab order works (trade mode), on the jobs' one counter; an exception while applying an IB order report is recorded as INTERNAL_ERROR (event and email) and stops the line's batch; an unexpected startup error reaches `logs/btlab.log` with its traceback (exit 1) instead of vanishing under `pythonw.exe`. Startup order, the lock, logging and shutdown were already as specified.
- §20.2 gap fixed: a result page offers "Use for a live portfolio" and, for runs with the ibguard rule while ibguard is configured, "Write to ibguard…"; both open the Live view with that backtest chosen. New locale keys in both languages; `sw.js` version updated. Checked in headless Chromium against the scratch UI server: the buttons, the preselection, its single use, no page errors.
- `README.md`: from a clean PC to a first backtest and a test-mode live portfolio, then trade mode, ibguard, iPhone and iPad through Tailscale Serve, IB Gateway, email, autostart, stopping, the drive letter, backups, the bench and §24.3, troubleshooting by code, and §25 in plain words.
- Tests: `tests/test_autostart.py` (8 checks of the script's text and its launcher, `run.bat`, and on Windows PowerShell's own parser on both scripts, skipped here on Linux), keep-awake while an order works, the failing report, the startup error in the log. Full suite: 1282 passed, 1 skipped (the Windows-only parse), also under `-X dev -W error`.
- For the owner: the parse test runs on the PC because `powershell.exe` is there; if it fails, the message names the line. The manual checklist above covers §24.3 item 8.
