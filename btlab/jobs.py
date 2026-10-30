"""Jobs: one per lane, data updates, backtests and optimizations (SPEC.md §17), and keep-awake (§23).

This module composes the data clients, prices.py, the store, backtest.py and optimize.py, and holds no logic of
theirs. Jobs stop cooperatively: a cancel or a shutdown ends a job between its steps, and its sleeps and waits at once.
Store reads and writes that may take a while run in threads, so the event loop stays free.
"""

import asyncio
import ctypes
import logging
import math
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np

from btlab import finmind, fred, tiingo
from btlab.backtest import (BacktestResult, RunConfig, RunConfigError, RunError, cost_model, load_run, lookahead,
                            run_backtest, save_result, stored_series, strategy_factory, validate_config)
from btlab.calendar import next_session
from btlab.config import OrdersConfig, ScheduleConfig, ServiceConfig
from btlab.finmind import FinMindClient
from btlab.fred import FredClient
from btlab.http import RequestBudget, Requester, SourceError, fetch_start, redact
from btlab.ib_link import IbError, IbLink
from btlab.live import (Bar, Book, batch_lines, corporate_actions, cutoff, fill_price, live_lot, replay_targets, settle,
                        target_weights)
from btlab.markets import MARKETS, category_by_rule
from btlab.notifier import Notifier, Silent
from btlab.optimize import NeedsConfirmation, OptimizeError, PoolFactory, Request, optimize, parse_request
from btlab.plugins import Plugin
from btlab.prices import ActionIssue, normalize_minutes, normalize_tw_actions, normalize_us_actions, share_ratio_after
from btlab.records import (Action, BacktestRow, Contract, DailyBars, ExternalSnapshot, LedgerRow, Line, LiveBatch, LiveOrder,
                           LivePortfolio, MinuteBars, MinuteCoverage, to_date, to_day)
from btlab.scheduler import due_sweep, due_updates, exhausted
from btlab.spylead_client import SOURCE as SPYLEAD, SpyleadClient
from btlab.store import MinuteStore, Store, StoreError
from btlab.taxes import TaxTable
from btlab.tiingo import TiingoClient
from btlab.trading import WORKING, TradeError, Trader

LANES = {"UPDATE_US": "DATA", "UPDATE_TW": "DATA", "SPYLEAD_SNAPSHOT": "DATA", "MINUTE_BACKFILL": "MINUTE",
         "BACKTEST": "COMPUTE", "OPTIMIZE": "COMPUTE", "LIVE_GENERATE": "LIVE", "LIVE_VIRTUAL_FILL": "LIVE",
         "LIVE_SEND": "LIVE", "LIVE_CANCEL": "LIVE", "LIVE_SWEEP": "LIVE"}
# The last session day swept in trade mode, per market (§15.6).
SWEEP_KEY = "live_sweep/{market}"
# The replay's and the benchmark's curves of a live portfolio, from its last batch (§14.6).
CURVES_KEY = "live_curves/{portfolio}"
# data_start: 30 years plus one year of indicator warm-up (§4.1).
DATA_YEARS = 31
TIINGO_SYMBOL_WARNING = 450
# Enough of a kind's latest runs to see a due time's run and its 3 retries.
RUNS_SEEN = 10
TAIPEI = ZoneInfo("Asia/Taipei")
_END_EVENTS = {
    "UPDATED": ("INFO", "UPDATE_FINISHED"), "PARTIAL": ("WARNING", "UPDATE_FINISHED"),
    "MAINTENANCE": ("WARNING", "UPDATE_FINISHED"), "UP_TO_DATE": ("INFO", "UP_TO_DATE"), "FAILED": ("ERROR", "JOB_FAILED"),
    "CANCELLED": ("WARNING", "JOB_CANCELLED"), "INTERRUPTED": ("WARNING", "JOB_INTERRUPTED"),
    # §18 has no kind of its own for an optimization that waits for the owner's confirmation.
    "NEEDS_CONFIRMATION": ("INFO", "JOB_CANCELLED"),
}
_SAVED_EVENTS = {"BACKTEST": "BACKTEST_SAVED", "OPTIMIZE": "OPTIMIZATION_SAVED", "LIVE_GENERATE": "BATCH_PROPOSED",
                 "LIVE_SEND": "ORDER_SENT", "LIVE_CANCEL": "ORDER_ENDED", "LIVE_SWEEP": "ORDER_ENDED"}
# What a job's progress and record show; the UI localizes each.
PHASES = ("CALENDAR", "INFO", "BARS", "ACTIONS", "RATES", "SNAPSHOT", "CONNECT", "MINUTES", "LOAD", "CHECK", "SIMULATE",
          "SAVE")
PERMISSION_KEY = "ib_permission/{market}"
OUTCOMES = (*_END_EVENTS, "DONE")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Keys:
    """The data sources' secrets (§22); an empty one fails its requests with MISSING_KEY."""

    tiingo: str = field(repr=False)
    finmind: str = field(repr=False)
    fred: str = field(repr=False)


class LiveError(Exception):
    """A live request the state of the portfolio refuses; code is the API's (§19)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JobRunning(Exception):
    """The lane already runs a job: the API's 409 JOB_RUNNING (§17.1)."""

    def __init__(self, lane: str) -> None:
        self.lane = lane
        super().__init__(f"the {lane} lane already runs a job")


class _Stopped(Exception):
    """The job was cancelled or interrupted."""


class Job:
    """One job: its progress for the state document (§17.1), and how it stops. ready resolves once the job knows
    whether it goes on: an optimization's NeedsConfirmation, or None."""

    def __init__(self, kind: str, started_at: datetime, sleep: Callable[[float], Awaitable[None]],
                 scheduled_for: datetime | None = None, market: str | None = None) -> None:
        self.kind, self.lane, self.started_at, self.scheduled_for = kind, LANES[kind], started_at, scheduled_for
        self.market = market
        self.phase: str | None = None
        self.done = self.total = 0
        self.item: str | None = None
        self.message: str | None = None
        self.stop_reason: str | None = None
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._sleep = sleep

    def progress(self) -> dict:
        return {"kind": self.kind, "phase": self.phase, "done": self.done, "total": self.total, "item": self.item,
                "started_at": self.started_at.isoformat(), "message": self.message}

    def begin(self, phase: str, total: int = 0) -> None:
        self.check()
        self.phase, self.done, self.total, self.item, self.message = phase, 0, total, None, None

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self._stop.set()

    def stopping(self) -> bool:
        """Read from an optimizer's thread too."""
        return self.stop_reason is not None

    def check(self) -> None:
        if self.stop_reason is not None:
            raise _Stopped

    async def race(self, awaitable: Awaitable) -> Any:
        """awaitable's result, unless the job stops first; a thread or a pool worker then finishes alone."""
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if self.stop_reason is not None:
            if not work.cancel() and not work.cancelled():
                work.exception()  # retrieved: the job stops whatever the work did
            raise _Stopped
        return work.result()

    async def sleep(self, seconds: float) -> None:
        await self.race(self._sleep(seconds))


class KeepAwake:
    """Windows stays awake while any holder needs it (§23): SetThreadExecutionState, called from the event loop's
    thread, which owns the state. Elsewhere it does nothing."""

    CONTINUOUS, SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self, set_state: Callable[[int], object] | None = None) -> None:
        self._set_state = set_state or _execution_state
        self.holders = 0

    def hold(self) -> None:
        self.holders += 1
        if self.holders == 1:
            self._set_state(self.CONTINUOUS | self.SYSTEM_REQUIRED)

    def release(self) -> None:
        self.holders -= 1
        if self.holders == 0:
            self._set_state(self.CONTINUOUS)


def _execution_state(flags: int) -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(flags)


class Jobs:
    """The lanes (§17.1): starting, following, stopping and recording jobs. Start them from the event loop.

    plugins gives the loaded plugins; pool makes the process pool of §13.7 with workers workers; session makes the
    aiohttp session of one job (§5.7); clock gives UTC epoch seconds. spylead with an empty url is not configured, and
    without ib there are no minute bars (§17.3).
    """

    def __init__(
        self, store: Store, minute_store: MinuteStore | None, keys: Keys, taxes: TaxTable | None,
        plugins: Callable[[], Mapping[str, Plugin]], pool: PoolFactory, workers: int, *,
        notifier: Notifier | None = None, session: Callable[[], Any] = aiohttp.ClientSession,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        keep_awake: KeepAwake | None = None, spylead: ServiceConfig = ServiceConfig("", ""),
        ib: IbLink | None = None, backfill_years: int = 3, mode: str = "test",
        orders: OrdersConfig = OrdersConfig("MOO", 1.0, 3.0), trader: Trader | None = None,
    ) -> None:
        self.store, self.minute_store, self.keys, self.taxes = store, minute_store, keys, taxes
        self.plugins, self.pool, self.workers, self.spylead = plugins, pool, workers, spylead
        self.ib, self.backfill_years, self.mode, self.orders, self.trader = ib, backfill_years, mode, orders, trader
        self.notifier = notifier or Silent()
        self.session, self.clock, self.sleep = session, clock, sleep
        self.keep_awake = keep_awake or KeepAwake()
        self._jobs: dict[str, Job] = {}
        self._orders_held = False

    def running(self) -> dict[str, dict]:
        """Each busy lane's job progress."""
        return {lane: job.progress() for lane, job in self._jobs.items()}

    def update(self, market: str, scheduled_for: datetime | None = None) -> Job:
        """UPDATE_US or UPDATE_TW (§17.2); scheduled_for is the due time a scheduled update answers."""
        return self._start(f"UPDATE_{market}", lambda job: _Update(self, job, market).run(), scheduled_for, market)

    def snapshot(self) -> Job:
        """SPYLEAD_SNAPSHOT (§5.6), on the DATA lane."""
        return self._start("SPYLEAD_SNAPSHOT", self._snapshot)

    def backfill(self, market: str | None = None) -> Job:
        """MINUTE_BACKFILL (§17.3) of one market's minute symbols, or of both markets'. Needs ib."""
        markets = tuple(MARKETS) if market is None else (market,)
        return self._start("MINUTE_BACKFILL", lambda job: _Backfill(self, job, markets).run())

    def generate(self, market: str) -> Job:
        """LIVE_GENERATE (§14.2) for the market's active portfolio of the run mode."""
        return self._start("LIVE_GENERATE", lambda job: _Generate(self, job, market).run(), market=market)

    def virtual_fill(self, market: str) -> Job:
        """LIVE_VIRTUAL_FILL (§14.5) for the market's active test-mode portfolio."""
        return self._start("LIVE_VIRTUAL_FILL", lambda job: _VirtualFill(self, job, market).run(), market=market)

    def create_portfolio(self, backtest: BacktestRow, capital: float | None) -> int:
        """A portfolio following a saved daily backtest from the next session (§14.1). LiveError NOT_DAILY or
        ALREADY_EXISTS."""
        if backtest.timeframe != "1d":
            raise LiveError("NOT_DAILY", "live portfolios follow daily backtests only")
        market = backtest.market
        first_day = self.next_session(market)
        try:
            return self.store.create_portfolio(market, self.mode, backtest.id, backtest.config,
                                               backtest.config["initial_capital"] if capital is None else capital,
                                               self.now(), first_day)
        except StoreError as error:
            if not error.conflict:
                raise
            raise LiveError("ALREADY_EXISTS", f"{market} already has an active {self.mode}-mode portfolio") from None

    def regenerate(self, portfolio: LivePortfolio) -> Job:
        """A new batch now (§14.3); LiveError BATCH_LOCKED once the next session's batch is confirmed, NOT_CONNECTED
        in trade mode without IB."""
        self._tradeable()
        latest = self.store.batches(portfolio.id, 1)
        if latest and latest[0].status != "PROPOSED" and latest[0].session_day >= self.next_session(portfolio.market):
            raise LiveError("BATCH_LOCKED", f"batch {latest[0].id} is {latest[0].status} and is never changed")
        return self.generate(portfolio.market)

    def confirm(self, batch: LiveBatch, portfolio: LivePortfolio, lines: Collection[int]) -> Job | None:
        """The owner's confirmation (§14.4): the chosen lines are confirmed and the others SKIPPED_OWNER. Test mode
        records them as VIRTUAL_PENDING (§14.5); trade mode checks the batch again and sends it in a LIVE_SEND job
        (§15.5). LiveError BATCH_EXPIRED unless the batch is PROPOSED and before its cutoff, and in trade mode
        NOT_CONNECTED or BATCH_STALE."""
        market = portfolio.market
        if batch.status != "PROPOSED" or self.now() >= cutoff(market, batch.session_day):
            raise LiveError("BATCH_EXPIRED", f"batch {batch.id} can no longer be confirmed")
        if self.mode == "trade":
            self._tradeable()
            try:
                self.trader.precheck(batch, market)
            except TradeError as refused:
                raise LiveError(refused.code, str(refused)) from None
        self.store.confirm_batch(batch.id, lines, "VIRTUAL_PENDING" if self.mode == "test" else "CONFIRMED", self.now())
        chosen = sum(line.status == "PROPOSED" and line.id in lines for line in batch.lines)
        self.event("INFO", "BATCH_CONFIRMED", f"batch {batch.id} confirmed with {chosen} lines", market)
        if self.mode != "trade":
            return None
        return self._start("LIVE_SEND", lambda job: self.trader.send(self.store.batch(batch.id), portfolio), market=market)

    def _tradeable(self) -> None:
        if self.mode == "trade" and self.trader is None:
            raise LiveError("NOT_CONNECTED", "trade mode needs IB: [ib] host is empty in config.toml")

    def cancel_orders(self, batch: LiveBatch, market: str) -> Job:
        """LIVE_CANCEL: btlab's own working orders of a batch (§15.8)."""
        return self._start("LIVE_CANCEL", lambda job: self.trader.cancel(batch), market=market)

    def sweep(self, market: str | None = None) -> Job:
        """LIVE_SWEEP (§15.6, §16.2): after a session, at start, and while a restore fails."""
        return self._start("LIVE_SWEEP", lambda job: self._sweep(market), market=market)

    async def _sweep(self, market: str | None) -> tuple[str, str]:
        await self.ib.connect()
        return await self.trader.sweep(market)

    def expire(self) -> None:
        """A PROPOSED batch whose cutoff passed is EXPIRED (§14.4, §17.4)."""
        now = self.now()
        for batch in self.store.batches_with_status("PROPOSED"):
            market = self.store.portfolio(batch.portfolio_id).market
            if now >= cutoff(market, batch.session_day):
                self.store.set_batch_status(batch.id, "EXPIRED")
                self.event("INFO", "BATCH_EXPIRED", f"batch {batch.id} for {to_date(batch.session_day)} expired unconfirmed",
                           market)

    def _sweeps(self) -> None:
        """15 minutes after each session, and every minute while an ended order's protection is not restored."""
        for market in MARKETS:
            day = due_sweep(market, self.now(), self.store.setting(SWEEP_KEY.format(market=market)))
            if day is not None and "LIVE" not in self._jobs:
                self.store.save_setting(SWEEP_KEY.format(market=market), day)
                self.sweep(market)
        if "LIVE" not in self._jobs and self.trader.unresolved():
            self.sweep()

    def _awake_for_orders(self) -> None:
        """Windows also stays awake while a btlab order works (§23): one hold on the jobs' own counter, because a
        second counter's release would clear this one's request."""
        working = bool(self.store.orders_with_status(*WORKING))
        if working and not self._orders_held:
            self.keep_awake.hold()
        elif self._orders_held and not working:
            self.keep_awake.release()
        self._orders_held = working

    def next_session(self, market: str) -> int:
        """The session after the latest stored one (§4.3, §14.3)."""
        stored = self.store.daily_bars("US", "SPY").day if market == "US" else self.store.trading_days(market)
        return next_session(stored, self.store.market_last_day(market) or int(stored[-1]))

    def permission(self, market: str) -> str | None:
        """OK or NO_PERMISSION as IB last answered for the market's minute bars, or None (§5.4)."""
        return self.store.setting(PERMISSION_KEY.format(market=market))

    def lane_job(self, lane: str) -> Job | None:
        return self._jobs.get(lane)

    async def take_snapshot(self, job: Job, session: Any) -> int:
        """SNAPSHOT: spylead's series replace the stored set in one transaction, and their count is returned. When
        spylead fails, the previous snapshot stays, an event SPYLEAD_UNREACHABLE records why, and the error goes on."""
        job.begin("SNAPSHOT")
        requester = Requester(SPYLEAD, session, sleep=job.sleep, clock=self.clock)
        try:
            series = await SpyleadClient(self.spylead.url, self.spylead.token, requester).snapshot()
        except SourceError as error:
            self.event("WARNING", "SPYLEAD_UNREACHABLE", str(error))
            raise
        self.store.replace_external_series(ExternalSnapshot(self.now(), series))
        return len(series)

    def backtest(self, raw: Any) -> Job:
        """Validates the configuration first: RunConfigError is the API's 400 INVALID_FIELD."""
        plugins = self.plugins()
        config = validate_config(raw, plugins, stored_series(self.store))
        return self._start("BACKTEST", lambda job: self._backtest(job, config, plugins))

    def optimization(self, raw: Any) -> Job:
        """Checks the request first: RunConfigError (INVALID_FIELD) or OptimizeError (TOO_MANY_COMBOS)."""
        request = parse_request(raw, self.plugins(), stored_series(self.store))
        return self._start("OPTIMIZE", lambda job: self._optimize(job, request))

    def cancel(self, lane: str) -> bool:
        """False when the lane runs no job."""
        job = self._jobs.get(lane)
        if job is not None:
            job.stop("CANCELLED")
        return job is not None

    async def interrupt(self) -> None:
        """At shutdown: every job stops as INTERRUPTED (§17.1)."""
        jobs = list(self._jobs.values())
        for job in jobs:
            job.stop("INTERRUPTED")
        await asyncio.gather(*(job.task for job in jobs))

    def tick(self, schedule: ScheduleConfig) -> Job | None:
        """Expires batches past their cutoff, keeps the PC awake for trade mode's working orders and starts its
        sweeps, then the first scheduled update that is due (§17.4); a due update waits while the DATA lane is busy."""
        self.expire()
        if self.trader is not None:
            self._awake_for_orders()
            self._sweeps()
        if not schedule.enabled or "DATA" in self._jobs:
            return None
        runs = {market: self.store.latest_job_runs(f"UPDATE_{market}", RUNS_SEEN) for market in MARKETS}
        due = due_updates(self.now(), {"US": schedule.us_update, "TW": schedule.tw_update}, runs)
        return self.update(due[0].market, due[0].scheduled_for) if due else None

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def event(self, level: str, kind: str, message: str, market: str | None = None, symbol: str | None = None) -> None:
        self.store.append_event(self.now(), level, kind, message, market, symbol)

    def _start(self, kind: str, work: Callable[[Job], Awaitable[tuple[str, str | None]]],
               scheduled_for: datetime | None = None, market: str | None = None) -> Job:
        lane = LANES[kind]
        if lane in self._jobs:
            raise JobRunning(lane)
        job = Job(kind, self.now(), self.sleep, scheduled_for, market)
        self._jobs[lane] = job
        self.keep_awake.hold()
        job.task = asyncio.get_running_loop().create_task(self._run(job, work))
        return job

    async def _run(self, job: Job, work: Callable[[Job], Awaitable[tuple[str, str | None]]]) -> None:
        """Runs a job and records its end, whatever happens (§17.1, §23)."""
        run_id = self.store.start_job_run(job.lane, job.kind, job.started_at, job.scheduled_for)
        market = job.market
        try:
            outcome, message = await work(job)
        except _Stopped:
            outcome, message = job.stop_reason, None
        except SourceError as error:
            outcome, message = "FAILED", str(error)
        except (RunError, OptimizeError) as error:
            outcome, message = "FAILED", f"{error.code}: {error}"
        except RunConfigError as error:
            # A live portfolio's stored configuration no longer validates, say a plugin was removed.
            outcome, message = "FAILED", f"INVALID_FIELD: {error}"
        except IbError as error:
            outcome, message = "FAILED", f"IB_{error.kind}: {error}"
        except Exception as error:
            log.exception("%s failed", job.kind)
            outcome, message = "FAILED", redact(f"{type(error).__name__}: {error}")
            self.event("ERROR", "INTERNAL_ERROR", f"{job.kind}: {message}", market)
            await self.notifier.send("INTERNAL_ERROR", error=message)
        finally:
            del self._jobs[job.lane]
            self.keep_awake.release()
            if not job.ready.done():
                job.ready.set_result(None)
        if self.trader is not None:
            # Before anything else runs on the loop, so a sent order's hold follows its job's without a pause.
            self._awake_for_orders()
        self.store.finish_job_run(run_id, self.now(), outcome, message)
        level, kind = ("INFO", _SAVED_EVENTS[job.kind]) if outcome == "DONE" else _END_EVENTS[outcome]
        self.event(level, kind, f"{job.kind} {outcome}" + (f": {message}" if message else ""), market)
        if job.kind.startswith("UPDATE_") and outcome in ("UPDATED", "PARTIAL"):
            self._follow_up(market)
        if job.kind == "LIVE_VIRTUAL_FILL" and outcome in ("UPDATED", "UP_TO_DATE"):
            self._live(self.generate, market)
        if outcome == "FAILED" and job.scheduled_for is not None:
            if exhausted(self.store.latest_job_runs(job.kind, RUNS_SEEN), job.scheduled_for):
                await self.notifier.send("UPDATE_FAILED", market=market, error=message)

    def _follow_up(self, market: str) -> None:
        """After a market's update (§17.2): the virtual fills of the session just stored, then a new batch (test mode);
        and the minute bars: IB may be asked again (§5.4), and a backfill fetches the new sessions, or a running one
        picks them up after its pause."""
        if self.store.active_portfolio(market, self.mode) is not None and (self.mode == "test" or self.trader is not None):
            self._live(self.virtual_fill if self.mode == "test" else self.generate, market)
        if self.ib is None:
            return
        if self.permission(market) == "NO_PERMISSION":
            self.store.save_setting(PERMISSION_KEY.format(market=market), None)
        if "MINUTE" not in self._jobs:
            self.backfill()

    def _live(self, start: Callable[[str], Job], market: str) -> None:
        """A live follow-up; a busy LIVE lane leaves it to the next update or a regenerate."""
        if "LIVE" in self._jobs:
            log.warning("LIVE lane busy: no %s follow-up for %s", start.__name__, market)
            return
        start(market)

    async def _snapshot(self, job: Job) -> tuple[str, str]:
        async with self.session() as session:
            count = await self.take_snapshot(job, session)
        return "UPDATED", f"series {count}"

    async def _backtest(self, job: Job, config: RunConfig, plugins: Mapping[str, Plugin]) -> tuple[str, str]:
        """LOAD, CHECK, SIMULATE (in a pool worker, with the metrics) and SAVE (§17.5)."""
        job.begin("LOAD")
        data = await job.race(asyncio.to_thread(load_run, self.store, self.minute_store, config))
        strategy = strategy_factory(config, plugins)
        job.begin("CHECK")
        suspect = await job.race(asyncio.to_thread(lookahead, config, data, strategy))
        job.begin("SIMULATE")
        result = await job.race(self.in_pool(run_backtest, config, data, self.taxes, strategy, False))
        if suspect is not None:
            result = replace(result, flags={**result.flags, "LOOKAHEAD_SUSPECT": suspect})
        job.begin("SAVE")
        backtest_id = await asyncio.to_thread(save_result, self.store, result, self.now())
        return "DONE", f"saved as backtest {backtest_id}"

    async def in_pool(self, function: Callable, *args: Any) -> Any:
        """function(*args) in a worker of the pool; when the job stops, a running worker finishes alone."""
        executor = self.pool(initializer=None, initargs=())
        try:
            future = executor.submit(function, *args)
            return await asyncio.to_thread(future.result)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _optimize(self, job: Job, request: Request) -> tuple[str, str]:
        """LOAD, then ESTIMATE, GRID and OOS or WFO in the optimizer, then SAVE (§17.5). A stop makes the optimizer
        return what it has, which is saved (§17.1)."""
        job.begin("LOAD")
        data = await job.race(asyncio.to_thread(load_run, self.store, self.minute_store, request.config))
        loop = asyncio.get_running_loop()

        def progress(phase: str, done: int, total: int) -> None:
            job.phase, job.done, job.total = phase, done, total
            if phase != "ESTIMATE":
                loop.call_soon_threadsafe(_settle, job.ready, None)

        found = await asyncio.to_thread(optimize, request, data, self.taxes, self.pool, self.workers,
                                        progress=progress, cancelled=job.stopping, clock=self.clock)
        if isinstance(found, NeedsConfirmation):
            _settle(job.ready, found)
            return "NEEDS_CONFIRMATION", f"the estimate of {found.estimate_seconds:.0f} s needs the owner's confirmation"
        status = job.stop_reason or found.status
        job.phase = "SAVE"
        config = request.config
        optimization_id = await asyncio.to_thread(
            self.store.save_optimization, self.now(), config.name, config.market, request.mode, status, request.document,
            found.results, found.best, found.flags,
        )
        return status, f"saved as optimization {optimization_id}"


def _settle(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


class _Update:
    """One market's data update (§17.2). Each symbol's requests either succeed or leave its error on its row."""

    def __init__(self, jobs: Jobs, job: Job, market: str) -> None:
        self.jobs, self.job, self.market, self.store = jobs, job, market, jobs.store
        self.now = jobs.now()
        self.today = to_day(self.now.astimezone(ZoneInfo(MARKETS[market].time_zone)).date())
        self.counts = {"bars": 0, "actions": 0, "suspensions": 0, "days": 0, "rates": 0}
        self.failed: list[str] = []

    async def run(self) -> tuple[str, str]:
        source, key = ("Tiingo API key", self.jobs.keys.tiingo) if self.market == "US" else ("FinMind token", self.jobs.keys.finmind)
        if not key:
            raise SourceError("MISSING_KEY", f"the {source} is empty")
        self._event("INFO", "UPDATE_STARTED", f"UPDATE_{self.market} started")
        today = to_date(self.today)
        data_start = self.store.set_data_start(self.market, to_day(_years_before(today, DATA_YEARS)))
        symbols = self._symbols()
        async with self.jobs.session() as session:
            try:
                await (self._us if self.market == "US" else self._tw)(session, symbols, data_start)
            except SourceError as error:
                if error.code != "MAINTENANCE":
                    raise
                return "MAINTENANCE", str(error)
            await self._rates(session, data_start)
            if self.jobs.spylead.url:
                await self._snapshot(session)
        return self._outcome(symbols)

    def _symbols(self) -> list[str]:
        """The market's benchmark, then every list's symbols (§7, §4.1); a new one is added with its category by rule."""
        symbols = [MARKETS[self.market].benchmark, *(s for found in self.store.lists(self.market) for s in found.symbols)]
        symbols = list(dict.fromkeys(symbols))
        for symbol in symbols:
            self.store.add_symbol(self.market, symbol, category_by_rule(self.market, symbol), self.now)
        return symbols

    def _requester(self, session: Any, source: str, budget_key: str | None = None,
                   limits: tuple[tuple[float, int], ...] = ()) -> Requester:
        """A source's requester for this job, with its budget loaded from the store and saved after each request."""
        budget = None
        if budget_key is not None:
            budget = RequestBudget(limits, self.store.request_log(budget_key),
                                   lambda times: self.store.save_request_log(budget_key, times))
        return Requester(source, session, budget=budget, sleep=self.job.sleep, clock=self.jobs.clock,
                         on_wait=lambda until: self._waiting(source, until))

    def _waiting(self, source: str, until: float) -> None:
        local = datetime.fromtimestamp(until, TAIPEI)
        self.job.message = f"waiting for {source}'s request limit until {local:%H:%M}"

    async def _us(self, session: Any, symbols: list[str], data_start: int) -> None:
        """BARS: one Tiingo request per symbol gives its bars and its actions (§5.1)."""
        client = TiingoClient(self._requester(session, "Tiingo", "tiingo", tiingo.LIMITS), self.jobs.keys.tiingo)
        self.job.begin("BARS", len(symbols))
        for symbol in symbols:
            await self._symbol(symbol, self._us_symbol(client, symbol, data_start))

    async def _us_symbol(self, client: TiingoClient, symbol: str, data_start: int) -> None:
        last = self._last_day(symbol)
        first = fetch_start(data_start, last, tiingo.PRICE_OVERLAP_DAYS)
        self._count_tiingo_symbol(symbol)
        bars, raw = await client.daily(symbol, first, self.today)
        self._store_bars(symbol, bars)
        self._store_actions(symbol, first, *normalize_us_actions(raw), last)

    async def _tw(self, session: Any, symbols: list[str], data_start: int) -> None:
        """CALENDAR, INFO, the split and par-value tables, then each symbol's BARS and ACTIONS (§5.2)."""
        client = FinMindClient(self._requester(session, "FinMind", "finmind", finmind.LIMITS), self.jobs.keys.finmind)
        self.job.begin("CALENDAR")
        self.counts["days"] += self.store.replace_trading_days("TW", await client.trading_days())
        await self._info(client, symbols)
        windows = {symbol: self._windows(symbol, data_start) for symbol in symbols}
        self.job.begin("ACTIONS")
        first = min(window[2] for window in windows.values())
        splits = await client.split_prices(first, self.today)
        par_changes = await client.par_value_changes(first)
        self.job.begin("BARS", len(symbols))
        for symbol in symbols:
            mine = ([s for s in splits if s.symbol == symbol], [c for c in par_changes if c.symbol == symbol])
            await self._symbol(symbol, self._tw_symbol(client, symbol, windows[symbol], *mine))

    async def _tw_symbol(self, client: FinMindClient, symbol: str, window: tuple[int | None, int, int],
                         splits: list, par_changes: list) -> None:
        last, price_first, first = window
        self.job.phase = "BARS"
        self._store_bars(symbol, await client.prices(symbol, price_first, self.today))
        self.job.phase = "ACTIONS"
        results = await client.dividend_results(symbol, first, self.today)
        policies = await client.dividend_policies(symbol, first, self.today)
        reductions = await client.capital_reductions(symbol, first, self.today)
        suspensions = await client.short_suspensions(symbol, first, self.today)
        self._store_actions(symbol, first, *normalize_tw_actions(results, policies, splits, par_changes, reductions), last)
        inside = [s for s in suspensions if first <= s.start_day <= self.today]
        self.counts["suspensions"] += self.store.replace_short_suspensions(symbol, first, self.today, inside)

    async def _info(self, client: FinMindClient, symbols: list[str]) -> None:
        """INFO: names and exchanges, at most once a day (§17.2)."""
        if self.store.setting("stock_info_day/TW") == self.today:
            return
        self.job.begin("INFO")
        infos = {info.symbol: info for info in await client.stock_info()}
        for symbol in symbols:
            if symbol in infos:
                self.store.set_symbol_details("TW", symbol, infos[symbol].name, infos[symbol].exchange)
        self.store.save_setting("stock_info_day/TW", self.today)

    async def _rates(self, session: Any, data_start: int) -> None:
        """RATES: DTB3 from 30 days before the latest stored rate (§5.3). Its failure leaves the bars good: PARTIAL."""
        self.job.begin("RATES")
        client = FredClient(self._requester(session, "FRED"), self.jobs.keys.fred)
        stored = self.store.rf_rates().day
        first = fetch_start(data_start, int(stored[-1]) if len(stored) else None, fred.OVERLAP_DAYS)
        try:
            self.counts["rates"] += self.store.upsert_rf_rates(await client.dtb3(first))
        except SourceError as error:
            self.failed.append("DTB3")
            self._event("WARNING", "SYMBOL_FAILED", str(error), "DTB3")

    async def _snapshot(self, session: Any) -> None:
        """SNAPSHOT after the market's data (§17.2): spylead's failure is its event, and the market's data stay good."""
        try:
            await self.jobs.take_snapshot(self.job, session)
        except SourceError:
            pass

    async def _symbol(self, symbol: str, requests: Awaitable[None]) -> None:
        """One symbol's requests; a failure is stored on its row and the update goes on (§17.2)."""
        self.job.check()
        self.job.item, self.job.message = symbol, None
        try:
            await requests
        except SourceError as error:
            if error.code == "MAINTENANCE":
                raise
            self.failed.append(symbol)
            self.store.record_fetch(self.market, symbol, self.now, str(error))
            self._event("WARNING", "SYMBOL_FAILED", str(error), symbol)
        else:
            self.store.record_fetch(self.market, symbol, self.now, None)
        self.job.done += 1

    def _windows(self, symbol: str, data_start: int) -> tuple[int | None, int, int]:
        """The latest stored day, and the first day of the price and action requests (§5.2)."""
        last = self._last_day(symbol)
        return (last, fetch_start(data_start, last, finmind.PRICE_OVERLAP_DAYS),
                fetch_start(data_start, last, finmind.ACTION_OVERLAP_DAYS))

    def _last_day(self, symbol: str) -> int | None:
        found = self.store.daily_bar_range(self.market, symbol)
        return None if found is None else found[1]

    def _store_bars(self, symbol: str, bars: DailyBars) -> None:
        self.counts["bars"] += self.store.upsert_daily_bars(self.market, symbol, bars)

    def _store_actions(self, symbol: str, first: int, actions: list[Action], issues: list[ActionIssue],
                       last: int | None) -> None:
        """Replaces the actions of the request's window. Issues are recorded for days after the latest stored bar, so a
        window's old issue is not recorded again every day."""
        inside = [action for action in actions if first <= action.day <= self.today]
        self.counts["actions"] += self.store.replace_actions(self.market, symbol, first, self.today, inside)
        for issue in issues:
            if last is None or issue.day > last:
                self._event("WARNING", issue.kind, issue.message, symbol)

    def _count_tiingo_symbol(self, symbol: str) -> None:
        """Tiingo's free plan serves 500 distinct symbols a month; passing 450 records a warning once (§5.1)."""
        key = f"tiingo_symbols/{self.now:%Y-%m}"
        seen = self.store.setting(key) or []
        if symbol in seen:
            return
        self.store.save_setting(key, [*seen, symbol])
        if len(seen) == TIINGO_SYMBOL_WARNING:
            self._event("WARNING", "TIINGO_SYMBOL_BUDGET",
                        f"{len(seen) + 1} distinct symbols requested from Tiingo this month; the free plan allows 500")

    def _outcome(self, symbols: list[str]) -> tuple[str, str]:
        summary = ", ".join(f"{name} {count}" for name, count in self.counts.items() if count)
        if all(symbol in self.failed for symbol in symbols):
            return "FAILED", f"every symbol failed: {', '.join(self.failed)}"
        if self.failed:
            return "PARTIAL", f"failed: {', '.join(self.failed)}" + (f"; {summary}" if summary else "")
        return ("UPDATED", summary) if summary else ("UP_TO_DATE", "nothing new")

    def _event(self, level: str, kind: str, message: str, symbol: str | None = None) -> None:
        self.jobs.event(level, kind, message, self.market, symbol)


class _Backfill:
    """MINUTE_BACKFILL (§17.3): every uncovered symbol-day of the minute symbols, newest first, from IB, in raw prices
    (§5.4) and stored with its coverage. A market IB refuses gets no more requests until its next update; IB
    unreachable ends the job."""

    def __init__(self, jobs: Jobs, job: Job, markets: tuple[str, ...]) -> None:
        self.jobs, self.job, self.markets = jobs, job, markets
        self.store, self.minute_store = jobs.store, jobs.minute_store
        self.closes: dict[tuple[str, str], dict[int, float]] = {}
        self.exchanges: dict[tuple[str, str], str | None] = {}
        self.unresolved: set[tuple[str, str]] = set()
        self.counts: Counter = Counter()

    async def run(self) -> tuple[str, str]:
        self.job.begin("CONNECT")
        try:
            await self.job.race(self.jobs.ib.connect())
            await self._days()
        except IbError as error:
            return "FAILED", f"IB_UNREACHABLE: {error}"
        summary = ", ".join(f"{status} {count}" for status, count in sorted(self.counts.items()))
        return ("UPDATED", summary) if summary else ("UP_TO_DATE", "nothing to fetch")

    async def _days(self) -> None:
        queue = self._queue()
        self.job.begin("MINUTES", len(queue))
        started = self.jobs.clock()
        while queue:
            if await self._paused():
                # The update may have stored new sessions (§5.5).
                queue = self._queue()
                self.job.total = self.job.done + len(queue)
                continue
            await self._day(*queue.pop(0))
            self.job.done += 1
            self._estimate(started)

    async def _paused(self) -> bool:
        """The MINUTE lane waits while a DATA job runs (§17.1); True when it waited."""
        waited = False
        while (data := self.jobs.lane_job("DATA")) is not None:
            self.job.message, waited = "paused while a data job runs", True
            await self.job.race(asyncio.shield(data.task))
        return waited

    def _queue(self) -> list[tuple[int, str, str]]:
        """(day, market, symbol) of every uncovered day, newest first; a market IB refused is left out."""
        queue = []
        for market in self.markets:
            if self.jobs.permission(market) == "NO_PERMISSION":
                continue
            for info in self.store.symbols(market):
                key = (market, info.symbol)
                if info.minute_enabled and key not in self.unresolved:
                    self.exchanges[key] = info.exchange
                    queue += [(day, market, info.symbol) for day in self._uncovered(market, info.symbol)]
        return sorted(queue, key=lambda item: (-item[0], item[1], item[2]))

    def _uncovered(self, market: str, symbol: str) -> list[int]:
        """The days with a traded daily bar, from backfill_years before the latest one, whose coverage is missing or
        FAILED (§17.3)."""
        bars = self.store.daily_bars(market, symbol)
        if not len(bars.day):
            return []
        first = to_day(_years_before(to_date(int(bars.day[-1])), self.jobs.backfill_years))
        traded = (bars.day >= first) & ~bars.no_trade
        self.closes[(market, symbol)] = dict(zip(bars.day[traded].tolist(), bars.close[traded].tolist()))
        covered = {c.day for c in self.minute_store.minute_coverage(market, symbol, first) if c.status != "FAILED"}
        return [day for day in self.closes[(market, symbol)] if day not in covered]

    async def _day(self, day: int, market: str, symbol: str) -> None:
        self.job.check()
        self.job.item = f"{symbol} {to_date(day).isoformat()}"
        if self.jobs.permission(market) == "NO_PERMISSION" or (market, symbol) in self.unresolved:
            return
        try:
            contract = await self._contract(market, symbol)
            bars = await self.job.race(self.jobs.ib.minute_bars(market, symbol, contract, day))
        except IbError as error:
            if error.kind == "UNREACHABLE":
                raise
            self._refused(day, market, symbol, error)
        else:
            self._stored(day, market, symbol, bars)

    async def _contract(self, market: str, symbol: str) -> Contract:
        """From the store, else resolved and cached (§5.4). A symbol IB cannot resolve waits for the next run."""
        found = self.store.contract(market, symbol)
        if found is None:
            try:
                found = await self.job.race(self.jobs.ib.contract(market, symbol, self.exchanges[(market, symbol)]))
            except IbError as error:
                if error.kind == "FAILED":
                    self.unresolved.add((market, symbol))
                raise
            self.store.save_contract(market, symbol, found)
        return found

    def _refused(self, day: int, market: str, symbol: str, error: IbError) -> None:
        """IB's "no data" is EMPTY; NO_PERMISSION stops the market until its next update; anything else is FAILED and
        retried by a later run."""
        status = {"NO_DATA": "EMPTY", "NO_PERMISSION": "NO_PERMISSION"}.get(error.kind, "FAILED")
        self._save(market, symbol, MinuteCoverage(day, status, None, self.jobs.now()))
        if status == "FAILED":
            log.warning("minute bars %s %s: %s", market, symbol, error)
        if status == "NO_PERMISSION":
            self._permission(market, "NO_PERMISSION")
            self.jobs.event("WARNING", "MINUTE_NO_PERMISSION",
                            f"{error}; no {market} minute requests until the next {market} update", market, symbol)

    def _stored(self, day: int, market: str, symbol: str, bars: MinuteBars) -> None:
        ratio = share_ratio_after(self.store.actions(market, symbol), day)
        found, status, factor = normalize_minutes(bars, self.closes[(market, symbol)][day], ratio)
        self._save(market, symbol, MinuteCoverage(day, status, factor, self.jobs.now()), found)
        self._permission(market, "OK")
        if status == "MISMATCH":
            self.jobs.event("WARNING", "MINUTE_MISMATCH", f"{to_date(day).isoformat()}: the daily close is {factor:.4f} × "
                            "the last minute close; that day's exits use the daily path", market, symbol)

    def _save(self, market: str, symbol: str, coverage: MinuteCoverage, bars: MinuteBars | None = None) -> None:
        self.minute_store.save_minute_day(market, symbol, coverage, bars)
        self.counts[coverage.status] += 1

    def _permission(self, market: str, value: str) -> None:
        if self.jobs.permission(market) != value:
            self.store.save_setting(PERMISSION_KEY.format(market=market), value)

    def _estimate(self, started: float) -> None:
        """The end at the pace so far (§17.3)."""
        left = self.job.total - self.job.done
        if left > 0:
            now = self.jobs.clock()
            end = datetime.fromtimestamp(now + (now - started) / self.job.done * left, TAIPEI)
            self.job.message = f"ends about {end:%H:%M}"


def virtual_book(store: Store, portfolio: LivePortfolio) -> Book:
    """A test-mode portfolio's virtual book: its holdings and the cash of its last ledger row, or the capital before its
    first (§14.5)."""
    ledger = store.ledger(portfolio.id)
    return Book(ledger[-1].cash if ledger else portfolio.capital, store.holdings(portfolio.id))


class _Generate:
    """LIVE_GENERATE (§14.2): the stored configuration replayed through the latest session, its targets scaled to the
    live equity, and a PROPOSED batch for the next session. The replay's and the benchmark's curves are kept for the
    chart (§14.6)."""

    def __init__(self, jobs: Jobs, job: Job, market: str) -> None:
        self.jobs, self.job, self.market = jobs, job, market

    async def run(self) -> tuple[str, str]:
        jobs, market = self.jobs, self.market
        portfolio = jobs.store.active_portfolio(market, jobs.mode)
        if portfolio is None:
            return "UP_TO_DATE", "no active portfolio"
        self.job.begin("LOAD")
        latest = jobs.store.market_last_day(market)
        missing = [s for s in portfolio.config["symbols"] if (jobs.store.daily_bar_range(market, s) or (0, 0))[1] < latest]
        if missing:
            # A bar, or a no-trade day, on the latest session for every symbol (§17.2).
            message = f"no bar on {to_date(latest)} for {', '.join(missing)}; the next update tries again"
            jobs.event("WARNING", "LIVE_DATA_INCOMPLETE", message, market)
            return "FAILED", f"LIVE_DATA_INCOMPLETE: {message}"
        plugins = jobs.plugins()
        config = validate_config({**portfolio.config, "end": None}, plugins, stored_series(jobs.store))
        data = await self.job.race(asyncio.to_thread(load_run, jobs.store, jobs.minute_store, config))
        self.job.begin("SIMULATE")
        replay = await self.job.race(jobs.in_pool(run_backtest, config, data, jobs.taxes, strategy_factory(config, plugins), False))
        self.job.begin("SAVE")
        return await self._batch(portfolio, config, data, replay)

    async def _batch(self, portfolio: LivePortfolio, config: RunConfig, data: Any, replay: BacktestResult) -> tuple[str, str]:
        jobs, market = self.jobs, self.market
        aligned = data.aligned
        raw = _last_closes(aligned.symbols, aligned.close)
        # The replay's weights are on its own valuation, raw or adjusted like its equity (§10.2).
        valued = raw if config.price_mode == "REALISTIC" else _last_closes(aligned.symbols, aligned.adj_close)
        targets = replay_targets(replay.holdings, replay.pending)
        weights = target_weights(targets, {s: valued[s] or 0.0 for s in targets}, float(replay.curve.equity[-1]))
        session_day = jobs.next_session(market)
        offset = jobs.orders.us_limit_offset_pct if market == "US" else jobs.orders.tw_limit_offset_pct
        if jobs.mode == "trade":
            # The ledger catches up with the sessions just stored before IB's positions make the batch (§14.6).
            jobs.trader.catch_up(portfolio, aligned, config.dividend_withholding_pct)
            lines, current = await jobs.trader.lines(portfolio, weights, raw, session_day, jobs.orders.us_order_kind, offset)
        else:
            book = virtual_book(jobs.store, portfolio)
            lots = {s: live_lot(market, _size_increment(jobs.store.contract(market, s))) for s in config.symbols}
            equity = book.equity({s: close for s, close in raw.items() if close is not None})
            current = book.holdings
            lines = batch_lines(market, weights, raw, current, equity, lots, session_day, jobs.orders.us_order_kind, offset)
        batch_id = jobs.store.save_batch(portfolio.id, session_day, jobs.now(), current, lines)
        jobs.store.save_setting(CURVES_KEY.format(portfolio=portfolio.id), _curves(replay, portfolio.first_day))
        proposed = sum(line.status == "PROPOSED" for line in lines)
        await jobs.notifier.send("BATCH_READY", market=market, session=to_date(session_day).isoformat(),
                                 cutoff=f"{cutoff(market, session_day):%Y-%m-%d %H:%M %Z}", lines=str(proposed))
        return "DONE", f"batch {batch_id} for {to_date(session_day)}: {proposed} lines, {len(lines) - proposed} skipped"


class _VirtualFill:
    """LIVE_VIRTUAL_FILL (§14.5, §14.6): each stored session from the portfolio's first, or after its last ledger row:
    corporate actions at the open, the fills of the lines confirmed for that session, and the ledger row at the close.
    Confirmed lines whose session brought no bar expire."""

    def __init__(self, jobs: Jobs, job: Job, market: str) -> None:
        self.jobs, self.job, self.market = jobs, job, market

    async def run(self) -> tuple[str, str]:
        jobs = self.jobs
        portfolio = jobs.store.active_portfolio(self.market, "test")
        if portfolio is None:
            return "UP_TO_DATE", "no active portfolio"
        self.job.begin("LOAD")
        config = validate_config({**portfolio.config, "end": None}, jobs.plugins(), stored_series(jobs.store))
        data = await self.job.race(asyncio.to_thread(load_run, jobs.store, jobs.minute_store, config))
        ledger = jobs.store.ledger(portfolio.id)
        aligned = data.aligned
        rows = np.flatnonzero(aligned.days >= (ledger[-1].day + 1 if ledger else portfolio.first_day))
        if not len(rows):
            return "UP_TO_DATE", "no new session"
        costs = cost_model(config, data.categories, data.borrow_fee_pct, jobs.taxes)
        confirmed = [batch for batch in jobs.store.batches_with_status("CONFIRMED") if batch.portfolio_id == portfolio.id]
        book, counts = virtual_book(jobs.store, portfolio), Counter()
        self.job.begin("SAVE", len(rows))
        for row in rows.tolist():
            book, fills, done = _session(book, confirmed, aligned, row, costs, config.dividend_withholding_pct)
            confirmed = [batch for batch in confirmed if batch.id not in done]
            prices = _last_closes(aligned.symbols, aligned.close[:row + 1])
            ledger_row = LedgerRow(int(aligned.days[row]), book.cash, book.equity(prices), dict(book.holdings))
            jobs.store.save_session(portfolio.id, ledger_row, fills, done, jobs.now())
            counts.update(status for _, status, *_ in fills)
            self.job.done += 1
        return "UPDATED", f"sessions {len(rows)}, filled {counts['FILLED']}, expired {counts['EXPIRED']}"


def _session(book: Book, confirmed: list[LiveBatch], aligned: Any, row: int, costs: Any,
             withholding_pct: float) -> tuple[Book, list[tuple], list[int]]:
    """One session of the virtual book: the corporate actions at the open, then the fills of the lines confirmed for
    it; returns the book, each line's (id, status, quantity, price, costs) and the batches that ended."""
    day, symbols = int(aligned.days[row]), aligned.symbols
    before = _last_closes(symbols, aligned.close[:row])
    book = corporate_actions(book, {s: float(aligned.ratio[row, j]) for j, s in enumerate(symbols)},
                             {s: float(aligned.cash[row, j]) for j, s in enumerate(symbols)}, before, withholding_pct)
    fills, done = [], []
    for batch in confirmed:
        if batch.session_day > day:
            continue
        for order in batch.lines:
            if order.status != "VIRTUAL_PENDING":
                continue
            j = symbols.index(order.symbol)
            line = _trading_line(order, float(aligned.ratio[row, j]))
            bar = Bar(*(float(getattr(aligned, name)[row, j]) for name in ("open", "high", "low", "close")),
                      bool(aligned.tradable[row, j]), int(aligned.lock[row, j]))
            # A batch whose session brought no bar, a holiday, expires: day orders never carry over (§14.5).
            price = fill_price(line, bar) if batch.session_day == day else None
            if price is None:
                fills.append((order.id, "EXPIRED", 0.0, None, None))
                continue
            book, paid, charge = settle(book, line, price, costs, j, day)
            fills.append((order.id, "FILLED", line.quantity, paid, charge))
        done.append(batch.id)
    return book, fills, done


def _trading_line(order: LiveOrder, ratio: float) -> Line:
    """A confirmed line as it trades on its session: a share ratio that day scales its quantity and prices (§10.7)."""
    limit = None if order.limit_price is None else order.limit_price / ratio
    return Line(order.symbol, order.action, float(math.trunc(order.quantity * ratio)), order.kind, limit,
                order.ref_price / ratio, order.status)


def _last_closes(symbols: tuple[str, ...], values: np.ndarray) -> dict[str, float | None]:
    """Each symbol's latest close, carried over a no-trade day; None without any."""
    found = {}
    for j, symbol in enumerate(symbols):
        known = np.flatnonzero(np.isfinite(values[:, j]))
        found[symbol] = float(values[known[-1], j]) if len(known) else None
    return found


def _size_increment(contract: Contract | None) -> float | None:
    return None if contract is None else contract.size_increment


def _curves(replay: BacktestResult, first_day: int) -> dict:
    """The replay's equity and its benchmark from the session before the portfolio's first (§14.6)."""
    days = replay.series["TIME"].astype(np.int64)
    first = max(0, int(np.searchsorted(days, first_day)) - 1)
    return {"t": days[first:].tolist(),
            **{name: [None if not math.isfinite(v) else float(v) for v in replay.series[kind][first:]]
               for name, kind in (("replay", "EQUITY"), ("benchmark", "BENCHMARK"))}}


def _years_before(day: date, years: int) -> date:
    """29 February becomes 28 February."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)
