"""python -m btlab --config <path> (SPEC.md §22) and logging (SPEC.md §23)."""

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import btlab.__main__ as entry
from btlab.__main__ import LOCKED, main, run, setup_logging, take_lock
from btlab.config import load_config
from btlab.ib_link import IbLink
from btlab.taxes import load_taxes
from btlab.web import start_site
from fakes import FakeIB, FakeOrderPort, InlinePool
from test_config import edited, render

REPO = Path(__file__).resolve().parents[1]
TOKEN = "Sentinel-Web-Token-0123456789"

# The child imports this through its site module: any socket activity, even a DNS lookup, ends it at once.
NO_NETWORK = """
import os
import sys


def _no_sockets(event, args):
    if event.startswith("socket."):
        sys.stderr.write("NETWORK ACCESS: " + event + "\\n")
        sys.stderr.flush()
        os._exit(97)


sys.addaudithook(_no_sockets)
sys.stderr.write("network guard on\\n")
"""


@pytest.fixture
def logging_sandbox():
    """Calls a function, then removes the root-logger handlers it added and restores the root level."""
    root = logging.getLogger()
    level, added = root.level, []

    def call(function, *args):
        before = set(root.handlers)
        try:
            return function(*args)
        finally:
            added.extend(handler for handler in root.handlers if handler not in before)

    yield call, added
    for handler in added:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)


def config_dir(tmp_path: Path) -> Path:
    """A folder with a valid config.toml and the committed taxes.toml."""
    text = (REPO / "config.example.toml").read_text(encoding="utf-8").replace('"change-me"', f'"{TOKEN}"')
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    (tmp_path / "taxes.toml").write_bytes((REPO / "taxes.toml").read_bytes())
    return tmp_path


def test_the_example_config_fails_on_the_placeholder_token_without_network(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(NO_NETWORK, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(REPO)]), PYTHONIOENCODING="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "btlab", "--config", "config.example.toml"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert "network guard on" in done.stderr
    assert "NETWORK ACCESS" not in done.stderr
    assert done.returncode == 2
    assert "web.token is still the placeholder" in done.stderr


@contextmanager
def held_lock(path: Path) -> Iterator[None]:
    """Another process takes the lock and keeps it until it is killed, when the block ends."""
    with subprocess.Popen([sys.executable, "-c", HOLD, str(path)], cwd=REPO, stdout=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout.readline().strip() == "held"
            yield
        finally:
            holder.kill()


HOLD = """
import sys, time
from pathlib import Path
from btlab.__main__ import take_lock
lock = take_lock(Path(sys.argv[1]))
print("held" if lock else "busy", flush=True)
time.sleep(120)
"""


def test_main_exits_before_any_database_while_another_process_holds_the_lock(tmp_path, logging_sandbox):
    call, _ = logging_sandbox
    root = config_dir(tmp_path)
    with held_lock(root / "data" / "btlab.lock"):
        assert call(main, ["--config", str(root / "config.toml")]) == LOCKED
    assert sorted(path.name for path in (root / "data").iterdir()) == ["btlab.lock"]
    text = (root / "logs" / "btlab.log").read_text(encoding="utf-8")
    assert "tiingo.api_key is empty; its requests will fail with MISSING_KEY" in text
    assert "another btlab process is running" in text and TOKEN not in text


def test_a_lock_left_by_a_killed_process_does_not_block_a_restart(tmp_path):
    path = tmp_path / "data" / "btlab.lock"
    with held_lock(path):
        assert take_lock(path) is None
    lock = take_lock(path)  # the holder was killed
    assert lock is not None
    lock.close()


def test_startup_and_shutdown_follow_section_23(tmp_path, caplog):
    root = config_dir(tmp_path)
    config, taxes = load_config(root / "config.toml"), load_taxes(root / "taxes.toml")
    caplog.set_level(logging.INFO, logger="btlab")

    async def go():
        stop = asyncio.Event()
        stop.set()
        await run(config, taxes, stop, pool=InlinePool, serve=lambda app: start_site(app, 0))

    asyncio.run(go())
    assert [record.getMessage() for record in caplog.records if record.name == "btlab"] == [
        "stores open", "plugins loaded: 0, 0 failed", "web server listening on 127.0.0.1", "scheduler started; ready",
        "web server stopped", "jobs stopped", "stores closed",
    ]
    assert (root / "data" / "btlab.db").exists() and (root / "data" / "minute.db").exists()


@pytest.mark.parametrize("mode", ["test", "trade"])
def test_only_trade_mode_constructs_the_order_adapter(tmp_path, monkeypatch, mode):
    """§15.1: test mode never constructs ib_orders' adapter. IB is a fake here, so nothing connects anywhere."""
    document = edited(None, "mode", mode)
    if mode == "trade":
        document["limits"] = {"us_max_orders_per_day": 2, "tw_max_orders_per_day": 2, "max_order_pct_of_equity": 10.0,
                              "us_max_order_value": 1000.0, "tw_max_order_value": 30000.0}
    (tmp_path / "config.toml").write_text(render(document), encoding="utf-8")
    built = []
    monkeypatch.setattr(entry, "IbOrders", lambda ib: built.append(ib) or FakeOrderPort())
    monkeypatch.setattr(entry, "IbLink", lambda config, mode: IbLink(config, mode, FakeIB(time.time)))
    config, taxes = load_config(tmp_path / "config.toml"), load_taxes(REPO / "taxes.toml")

    async def go():
        stop = asyncio.Event()
        stop.set()
        await run(config, taxes, stop, pool=InlinePool, serve=lambda app: start_site(app, 0))

    asyncio.run(go())
    assert len(built) == (mode == "trade")


def test_main_rejects_a_missing_tax_table_before_logging(tmp_path, capsys):
    root = config_dir(tmp_path)
    (root / "taxes.toml").unlink()
    assert main(["--config", str(root / "config.toml")]) == 2
    error = capsys.readouterr().err
    assert "tax table" in error
    assert "the file does not exist" in error
    assert not (root / "logs").exists()


def test_main_needs_a_config_argument(capsys):
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    assert "--config" in capsys.readouterr().err


def test_logging_follows_section_23(tmp_path, logging_sandbox):
    call, added = logging_sandbox
    call(setup_logging, tmp_path / "logs")
    (file,) = [handler for handler in added if isinstance(handler, RotatingFileHandler)]
    (console,) = [handler for handler in added if type(handler) is logging.StreamHandler]
    assert logging.getLogger().level == logging.DEBUG
    assert Path(file.baseFilename) == tmp_path / "logs" / "btlab.log"
    assert (file.level, file.maxBytes, file.backupCount) == (logging.DEBUG, 5 * 1024 * 1024, 5)
    assert console.level == logging.INFO
    logging.getLogger("btlab.test").debug("a debug line")
    assert "a debug line" in (tmp_path / "logs" / "btlab.log").read_text(encoding="utf-8")


def test_log_lines_are_redacted(tmp_path, logging_sandbox):
    call, _ = logging_sandbox
    call(setup_logging, tmp_path / "logs")
    key = "Sentinel-Fred-Key-77aa"
    logger = logging.getLogger("btlab.test")
    logger.info("GET https://api.stlouisfed.org/fred/series/observations?series_id=DTB3&api_key=%s&file_type=json", key)
    try:
        raise RuntimeError(f"failed with headers {{'Authorization': 'Token {key}'}}")
    except RuntimeError:
        logger.exception("unexpected")
    text = (tmp_path / "logs" / "btlab.log").read_text(encoding="utf-8")
    assert key not in text
    assert "api_key=***&file_type=json" in text and "'Authorization': 'Token ***'" in text
