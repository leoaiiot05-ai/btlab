"""python -m btlab --config <path>: CLI, wiring and lifecycle (SPEC.md §22, §23)."""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

import aiohttp
from aiohttp import web

from btlab.config import Config, ConfigError, ScheduleConfig, load_config, startup_warnings
from btlab.http import redact
from btlab.ib_link import IbLink
from btlab.ib_orders import IbOrders
from btlab.ibguard_client import IbguardClient, connected
from btlab.jobs import Jobs, Keys
from btlab.notifier import Notifier, make_notifier
from btlab.optimize import PoolFactory, default_workers, process_pool
from btlab.plugins import PluginFolder
from btlab.store import MinuteStore, Store
from btlab.taxes import TaxTable, TaxTableError, load_taxes
from btlab.trading import Trader
from btlab.web import Services, make_app, start_site

LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 5
LOCK_FILE = "btlab.lock"
TICK_S = 60.0
# A second process exits with this code, before opening a database or the port.
LOCKED = 3

log = logging.getLogger("btlab")


def main(argv: list[str] | None = None) -> int:
    """Startup in the order of §23: config, tax table, the process lock, then the app until Ctrl+C."""
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        taxes = load_taxes(config.root / "taxes.toml")
    except (ConfigError, TaxTableError) as error:
        # Logging is not set up yet: logs/ sits next to a config file that may not be usable.
        print(f"btlab: {error}", file=sys.stderr)
        return 2
    setup_logging(config.root / "logs")
    for warning in startup_warnings(config):
        log.warning(warning)
    log.info("config and tax table loaded; mode %s", config.mode)
    lock = take_lock(config.root / "data" / LOCK_FILE)
    if lock is None:
        log.error("another btlab process is running on %s; exiting", config.root / "data")
        return LOCKED
    try:
        asyncio.run(run(config, taxes))
    except KeyboardInterrupt:
        pass
    except Exception:
        # Under pythonw.exe, which the autostart runs, there is no console: the log file is the only witness.
        log.exception("btlab stopped on an unexpected error")
        return 1
    finally:
        lock.close()
    return 0


def take_lock(path: Path) -> IO[bytes] | None:
    """An operating-system lock on path, held while the returned file stays open (§23): msvcrt on Windows, flock
    elsewhere. None when another process holds it. It dies with its process; the file's presence means nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


async def run(config: Config, taxes: TaxTable, stop: asyncio.Event | None = None, *, pool: PoolFactory | None = None,
              serve: Callable[[web.Application], Awaitable[web.AppRunner]] | None = None) -> None:
    """Both stores, the plugins, the jobs with the real pool, the web server and the scheduler, then serving until
    stop is set or Ctrl+C. Shutdown stops accepting requests, interrupts the jobs (their pools close with them) and
    closes the stores (§23)."""
    stop = stop or asyncio.Event()
    data = config.root / "data"
    store = Store(data)
    minute_store = MinuteStore(data)
    log.info("stores open")
    try:
        plugins = PluginFolder(config.root / "strategies")
        log.info("plugins loaded: %d, %d failed", len(plugins.plugins), len(plugins.errors))
        workers = default_workers()
        # IB connects on demand, when a job first needs it (§22).
        ib = IbLink(config.ib, config.mode) if config.ib.host else None
        notifier = make_notifier(config.email, config.mode)
        trader = _trader(config, store, ib, notifier) if config.mode == "trade" and ib is not None else None
        jobs = Jobs(store, minute_store, Keys(config.tiingo.api_key, config.finmind.token, config.fred.api_key), taxes,
                    lambda: plugins.plugins, pool or process_pool(workers), workers,
                    notifier=notifier, spylead=config.spylead, ib=ib, backfill_years=config.minute.backfill_years,
                    mode=config.mode, orders=config.orders, trader=trader)
        app = make_app(Services(config, store, minute_store, jobs, plugins, time.time, notifier=jobs.notifier))
        runner = await (serve or (lambda found: start_site(found, config.web.port)))(app)
        log.info("web server listening on 127.0.0.1")
        background = [asyncio.create_task(_schedule(jobs, config.schedule))]
        if trader is not None:
            # Recovery and the safety sweep at start and at every connect (§15.7, §16.2 step 7).
            ib.on_connect = trader.on_connect
            jobs.sweep()
            background.append(asyncio.create_task(trader.stay_connected()))
        log.info("scheduler started; ready")
        try:
            await stop.wait()
        finally:
            for task in background:
                task.cancel()
            await runner.cleanup()
            log.info("web server stopped")
            await jobs.interrupt()
            log.info("jobs stopped")
            if ib is not None:
                ib.close()
    finally:
        store.close()
        minute_store.close()
        log.info("stores closed")


def _trader(config: Config, store: Store, ib: IbLink, notifier: Notifier) -> Trader:
    """Trade mode's orders on the link's connection (§15); test mode never constructs them (§15.1)."""
    def guard() -> AbstractAsyncContextManager[IbguardClient]:
        return connected(config.ibguard, aiohttp.ClientSession, asyncio.sleep, time.time)

    return Trader(store, IbOrders(ib.port), ib, config.limits, ibguard=guard if config.ibguard.url else None,
                  notifier=notifier, clock=time.time)


async def _schedule(jobs: Jobs, schedule: ScheduleConfig) -> None:
    """A tick at once, for the catch-up, then every minute (§17.4). A failing tick is logged and the next one runs."""
    while True:
        try:
            jobs.tick(schedule)
        except Exception:
            log.exception("the scheduler's tick failed")
        await asyncio.sleep(TICK_S)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m btlab", description="btlab, the owner's backtesting lab")
    parser.add_argument("--config", type=Path, required=True, help="path to config.toml")
    return parser.parse_args(argv)


class _RedactingFormatter(logging.Formatter):
    """Masks keys and tokens in every line, tracebacks included (§5.7). The code never logs one; this is a second guard."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path) -> None:
    """Console at INFO and a rotating file at DEBUG (SPEC.md §23), with UTC timestamps."""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = _RedactingFormatter("%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    file = RotatingFileHandler(log_dir / "btlab.log", maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    handlers: list[logging.Handler] = [file]
    # pythonw.exe, which the autostart runs, has no console.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        handlers.append(console)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
