# btlab

Personal backtesting lab. One Python process on the owner's Windows 11 PC:

- it downloads daily bars (Tiingo for US, FinMind for Taiwan), the 3-month T-bill rate (FRED) and 1-minute bars (Interactive Brokers through `ib_async`);
- it backtests built-in, plugin and rule-builder strategies on one market at a time, optimizes them, and stores every run;
- it runs one live portfolio per market (test mode: virtual orders; trade mode: IB orders the owner confirms) and coordinates with ibguard, the owner's exit engine;
- it serves a PWA: Edge on the PC, Safari on iPhone and iPad through Tailscale Serve, with saved results viewable offline.

Documents:

- `SPEC.md` is the source of truth for behaviour. Read the sections a task names before writing code.
- `PROMPTS.md` is the build sequence.
- `TODO.md` holds everything deferred.
- `CLOUD.md` holds the rules for cloud sessions that continue the build.

## Rules

- Never weaken, skip or "simplify" a rule in SPEC.md without asking. Behaviour marked "intentional" is the owner's decision, even where it looks like a bug.
- Tests never open a network connection and never connect to IB. Tiingo, FinMind, FRED, spylead and ibguard are reached only through their client modules, and IB only through `ib_link.py` and `ib_orders.py`. Tests use fakes.
- Never start the app (`python -m btlab` or `python -m btlab.bench`), never contact Tiingo, FinMind, FRED, spylead or ibguard, and never connect to IB, unless I ask. Prove behaviour with tests.
- No look-ahead. A value used at bar t comes from data available at t's close, and fills come after the signal (SPEC.md §8, §9.7, §10.3). Prove it with truncation tests.
- Orders: only `ib_orders.py` places or cancels orders, test mode never constructs it, and nothing calls `reqGlobalCancel` (SPEC.md §15.1).
- Never write the web token, the session value, an API key or token (Tiingo, FinMind, FRED, spylead, ibguard) or the SMTP password to a log, an event, an API response, a payload, an email or an error message. FRED takes its key in the URL: never log a URL unredacted.
- Never commit `config.toml`, `data/`, `logs/` or any key.
- This PC also runs IB Gateway for ibguard. Tests never connect to ports 4001, 4002, 7496 or 7497.
- `buildctl/`, `run_prompts.py`, `build.cmd` and `.claude-runs/` belong to the build automation. Never edit them, and never list `buildctl/` or `.claude/settings.json` in `.gitignore`.

## Code style

- Small single-purpose functions. No dead code. No speculative abstraction (YAGNI).
- Follow existing patterns in this repo before inventing new ones.
- Comments explain why, never what.
- Type hints on public functions; dataclasses for data.
- The modules SPEC.md §2 marks "Pure" stay pure: no I/O, no clock reads, no logging, no aiohttp, no ib_async.
- Numerical code uses NumPy float64 and is deterministic. Fix every random seed.

## Dependencies

- **Python.** Approved: `numpy`, `aiohttp`, `tzdata`, `ib_async==2.1.0` with its own dependencies (runtime); `pytest` (dev). Use the standard library for everything else.
- **Frontend.** Apache ECharts 6.1.0, vendored at `btlab/static/vendor/echarts/`. Do not replace or upgrade it. No framework and no build step.
- **Lock.** Dependencies are locked with hashes in `pylock.toml` (SPEC.md §21). Never re-lock, upgrade, or install anything outside the lock unless I ask. CLOUD.md has the only exception, for cloud sessions.
- Ask before adding any other dependency, including dev tools.

## Output discipline

- Show diffs or changed files only. Never re-print unchanged code.
- No explanations, apologies, or summaries unless I ask.

## Planning

Every plan lists, before any code is written:

1. Steps
2. Extension points
3. Refactor seams
4. Deferred items (append them to `TODO.md`)

Then wait for my approval.

## Quality bar

Ship "good enough": correct, tested, readable. A phase is done when its tests pass and the acceptance criteria it names in SPEC.md §24.2 hold.

## Environment

- **Runtime.** Native Windows 11, Python 3.12+. The project lives on `E:\00_CODING\btlab`, an external drive that may be exFAT.
- **Portability.** Code must still import and pass tests on any OS: cloud sessions run Linux. Guard Windows-only calls.
- **Install.**
  1. `python -m venv .venv`
  2. `.venv/Scripts/python -m pip install --upgrade pip`
  3. `.venv/Scripts/python -m pip install -r pylock.toml`
- The project is not installed as a package. Run the tests and the app from the repository root.
- **Test.** `.venv/Scripts/python -m pytest -q`
- **Run.** `.venv/Scripts/python -m btlab --config config.toml`
- Forward slashes work in both PowerShell and Git Bash.
- When you edit files, use your file-editing tools, not `git apply`.
- **Git.** In an automated run, commits are made for you: by `run_prompts.py` on the PC, or by `buildctl/cloud_lease.py` in a cloud session (CLOUD.md). Follow the run's instructions.

## Libraries

- NumPy, aiohttp and ib_async are whatever `pylock.toml` pins. Check their APIs against the installed source, not memory. The ib_async names in SPEC.md were checked against 2.1.0; re-check them.
- Worker-pool functions are top-level and take plain arrays: Windows spawns workers.
- Time zones come from `zoneinfo`; Windows needs `tzdata`.
