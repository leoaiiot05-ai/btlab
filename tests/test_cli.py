"""python -m btlab --config <path> (SPEC.md §22) and logging (SPEC.md §23)."""

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from btlab.__main__ import main, setup_logging

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


def test_main_loads_the_config_and_the_tax_table_then_exits(tmp_path, logging_sandbox):
    call, _ = logging_sandbox
    root = config_dir(tmp_path)
    assert call(main, ["--config", str(root / "config.toml")]) == 1
    text = (root / "logs" / "btlab.log").read_text(encoding="utf-8")
    assert "tiingo.api_key is empty; its requests will fail with MISSING_KEY" in text
    assert "the app is not implemented yet" in text
    assert TOKEN not in text


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
