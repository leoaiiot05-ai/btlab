# TODO (deferred)

Nothing here is in scope for v1. Do not build any of it without being asked. Append new deferred items under the matching heading, one line each, with the reason or the seam it would use.

## Markets and data

- [ ] Mixed US and Taiwan portfolios in one backtest, with FX conversion. Seam: the run's `market` and the engine's single currency.
- [ ] Futures, options, crypto and FX.
- [ ] Delisted symbols, for survivorship-free backtests. Seam: lists, plus a source of delisted histories.
- [ ] Historical index members (S&P 500, Taiwan 50) imported from CSV. Seam: lists with membership periods.
- [ ] Taiwan minute bars from FinMind Sponsor, in case IB gives no Taiwan access. Seam: `HistoryPort`.
- [ ] Minute bars older than 3 years.
- [ ] Cash interest at the risk-free rate.
- [ ] Dividends on the pay date instead of the ex-date.
- [ ] US regulatory fees on sales (SEC fee, FINRA TAF) and IB's Taiwan clearing fees. Seam: the commission functions in `costs.py`.
- [ ] A preset for a Taiwan broker's commission (0.1425% × discount, minimum NT$20). Today it is typed per backtest.

## Analysis

- [ ] Monte Carlo resampling of trades.
- [ ] Margin and leverage.
- [ ] Rotations with shorts (long the top K, short the bottom K).
- [ ] Several live portfolios per market, with summed targets.

## Live trading

- [ ] Live trading of minute strategies.
- [ ] Taiwan orders through a Taiwan broker's API (for example Shioaji), because IB does not serve residents of Taiwan. Seam: `OrderPort`.
- [ ] Separate ibguard thresholds per market. Needs an ibguard change.
- [ ] A strategy's intrabar exits as resting orders at IB.

## Interface

- [ ] Native iOS or Windows apps. The JSON API is the seam.
- [ ] Push notifications (Telegram or web push). Seam: the notifier protocol.

## Operations

- [ ] Daily backup of `data/`.
- [ ] Run as a Windows service instead of a logon task.
- [ ] Reload `config.toml` and `taxes.toml` without a restart; today both are read once at startup (§23). Seam: `load_config` and `load_taxes`, called from `__main__.py`.
- [ ] Format checks for URLs and email addresses in `config.toml`; today a malformed one fails at its first use. Seam: the per-key validators in `config.py`.
- [ ] Retention for the `events` and `job_runs` tables; today both keep every row (a few thousand a year). Seam: `append_event` and `start_job_run` in `store.py`.
- [ ] Schema upgrades for the databases; today the store creates schema version 1 and refuses a file with a newer `user_version`. Seam: where `store._Database` reads `user_version`.
- [ ] `Retry-After` as an HTTP date; today it counts as the 60 s default. Seam: `_retry_after` in `http.py`.
- [ ] A graceful stop for the autostarted btlab (a tray icon or a button in Settings); today Ctrl+C works only in `run.bat`'s console, and the autostarted `pythonw.exe` is ended from Task Manager, which skips the orderly shutdown of §23 (safe, but a running job's row keeps no end). Seam: the `stop` event that `__main__.run` already takes.
- [ ] Wake the PC from sleep for a scheduled update (a wake timer); today a sleeping PC runs the missed update when it wakes. Seam: the autostart script's task settings, or a second scheduled task.

## Tests

- [ ] `tests/test_jobs.py::test_a_cancelled_optimization_keeps_its_partial_results` failed once in four full runs under `-X dev -W error` (its list of completed combinations), and passes alone and in every other run: its semaphore gate races the optimizer's worker threads. Make the stop point deterministic. Seam: the gated pool in that test.
