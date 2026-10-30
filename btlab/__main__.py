"""python -m btlab --config <path>: CLI, wiring and lifecycle (SPEC.md §22, §23)."""

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from btlab.config import ConfigError, load_config, startup_warnings
from btlab.http import redact
from btlab.taxes import TaxTableError, load_taxes

LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 5

log = logging.getLogger("btlab")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        load_taxes(config.root / "taxes.toml")
    except (ConfigError, TaxTableError) as error:
        # Logging is not set up yet: logs/ sits next to a config file that may not be usable.
        print(f"btlab: {error}", file=sys.stderr)
        return 2
    setup_logging(config.root / "logs")
    for warning in startup_warnings(config):
        log.warning(warning)
    log.info("config and tax table loaded; mode %s", config.mode)
    log.warning("the app is not implemented yet; exiting")
    return 1


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
