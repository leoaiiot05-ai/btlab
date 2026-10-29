"""Load and validate config.toml (SPEC.md §22).

The file holds secrets, so no message here quotes a value from it: messages name keys (§21).
"""

import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from btlab.markets import US

MODES = ("test", "trade")
LANGUAGES = ("zh-TW", "en")
PLACEHOLDER_TOKEN = "change-me"
MIN_TOKEN_LENGTH = 16
IBGUARD_CLIENT_ID = 17


class ConfigError(Exception):
    """config.toml cannot be used. The messages name keys and never quote values."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(f"config file {path}:" + "".join(f"\n  - {problem}" for problem in problems))


@dataclass(frozen=True)
class WebConfig:
    port: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class TiingoConfig:
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class FinMindConfig:
    token: str = field(repr=False)


@dataclass(frozen=True)
class FredConfig:
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ServiceConfig:
    """spylead or ibguard. An empty url disables the service."""

    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class IbConfig:
    """An empty host disables IB: no minute bars, no orders."""

    host: str
    port: int
    client_id: int
    account: str


@dataclass(frozen=True)
class MinuteConfig:
    backfill_years: int


@dataclass(frozen=True)
class OrdersConfig:
    us_order_kind: str
    us_limit_offset_pct: float
    tw_limit_offset_pct: float


@dataclass(frozen=True)
class Limits:
    """Trade-mode limits (SPEC.md §15.3). The owner chooses them, so there are no defaults."""

    us_max_orders_per_day: int
    tw_max_orders_per_day: int
    max_order_pct_of_equity: float
    us_max_order_value: float
    tw_max_order_value: float


@dataclass(frozen=True)
class ScheduleConfig:
    """Times of the automatic updates, in Asia/Taipei."""

    enabled: bool
    us_update: time
    tw_update: time


@dataclass(frozen=True)
class EmailConfig:
    """An empty smtp_host disables email."""

    smtp_host: str
    smtp_port: int
    username: str
    password: str = field(repr=False)
    sender: str
    recipient: str
    language: str


@dataclass(frozen=True)
class Config:
    path: Path
    mode: str
    web: WebConfig
    tiingo: TiingoConfig
    finmind: FinMindConfig
    fred: FredConfig
    spylead: ServiceConfig
    ibguard: ServiceConfig
    ib: IbConfig
    minute: MinuteConfig
    orders: OrdersConfig
    schedule: ScheduleConfig
    email: EmailConfig
    limits: Limits | None = None

    @property
    def root(self) -> Path:
        """taxes.toml, strategies/, data/ and logs/ sit next to the config file (SPEC.md §22)."""
        return self.path.parent


def load_config(path: Path) -> Config:
    """Raises ConfigError listing every problem of the file."""
    doc = _read_toml(path)
    problems = [f"unknown key {key}" for key in doc if key != "mode" and key not in _SECTIONS]
    mode = _value(doc, "mode", "mode", _choice(*MODES), problems)
    # [limits] is the one optional section; trade mode requires it below.
    sections = {name: _section(doc, name, problems) for name in _SECTIONS if name in doc or name != "limits"}
    if mode == "trade" and "limits" not in doc:
        *keys, last = _SECTIONS["limits"][1]
        problems.append(f"trade mode needs [limits] with {', '.join(keys)} and {last} above 0")
    if problems:
        raise ConfigError(path, problems)
    return Config(path=path.absolute(), mode=mode, **sections)


def startup_warnings(config: Config) -> list[str]:
    """Empty keys and tokens are allowed, but the requests of an enabled service then fail with MISSING_KEY."""
    keys = (
        ("tiingo.api_key", config.tiingo.api_key, True),
        ("finmind.token", config.finmind.token, True),
        ("fred.api_key", config.fred.api_key, True),
        ("spylead.token", config.spylead.token, bool(config.spylead.url)),
        ("ibguard.token", config.ibguard.token, bool(config.ibguard.url)),
    )
    return [f"{name} is empty; its requests will fail with MISSING_KEY" for name, value, used in keys if used and not value]


_POSITION = re.compile(r"\(at (line \d+, column \d+|end of document)\)")


def _read_toml(path: Path) -> dict[str, Any]:
    # "from None" everywhere: a chained exception's message could quote the file.
    try:
        # Windows editors may save a BOM, which TOML does not allow.
        return tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(path, ["the file does not exist"]) from None
    except OSError as error:
        raise ConfigError(path, [f"cannot be read ({error.strerror or type(error).__name__})"]) from None
    except UnicodeDecodeError:
        raise ConfigError(path, ["is not UTF-8 text"]) from None
    except tomllib.TOMLDecodeError as error:
        # tomllib's text can quote a character of the file; its position is enough to find the mistake.
        position = _POSITION.search(str(error))
        raise ConfigError(path, [f"is not valid TOML ({position[1] if position else 'position unknown'})"]) from None


class _Invalid(Exception):
    """A value breaks a rule. The reason never quotes the value."""


def _section(doc: dict[str, Any], name: str, problems: list[str]) -> Any:
    cls, checks = _SECTIONS[name]
    table = doc.get(name)
    if table is None:
        problems.append(f"section [{name}] is missing")
        return None
    if not isinstance(table, dict):
        problems.append(f"{name} must be a table, not {_kind(table)}")
        return None
    problems += [f"unknown key {name}.{key}" for key in table if key not in checks]
    found = len(problems)
    values = {key: _value(table, key, f"{name}.{key}", check, problems) for key, check in checks.items()}
    return cls(**values) if len(problems) == found else None


def _value(table: dict[str, Any], key: str, name: str, check: Callable[[Any], Any], problems: list[str]) -> Any:
    if key not in table:
        problems.append(f"{name} is missing")
        return None
    try:
        return check(table[key])
    except _Invalid as invalid:
        problems.append(f"{name} {invalid}")
        return None


_TOML_TYPES = {str: "a string", int: "an integer", float: "a number", bool: "a boolean", list: "an array", dict: "a table"}


def _kind(value: Any) -> str:
    return _TOML_TYPES.get(type(value), "a date or time")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise _Invalid(f"must be a string, not {_kind(value)}")
    return value


def _integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Invalid(f"must be an integer, not {_kind(value)}")
    return value


def _number(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _Invalid(f"must be a number, not {_kind(value)}")
    if not math.isfinite(value):
        raise _Invalid("must be a finite number")
    return float(value)


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _Invalid(f"must be true or false, not {_kind(value)}")
    return value


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if _text(value) not in options:
            raise _Invalid("must be " + " or ".join(f'"{option}"' for option in options))
        return value

    return check


def _port(value: Any) -> int:
    if not 1 <= _integer(value) <= 65535:
        raise _Invalid("must be a port number from 1 to 65535")
    return value


def _positive_integer(value: Any) -> int:
    if _integer(value) <= 0:
        raise _Invalid("must be above 0")
    return value


def _positive_number(value: Any) -> float:
    number = _number(value)
    if number <= 0:
        raise _Invalid("must be above 0")
    return number


def _offset_pct(value: Any) -> float:
    number = _number(value)
    if not 0 <= number < 100:
        raise _Invalid("must be at least 0 and below 100")
    return number


_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def _clock(value: Any) -> time:
    match = _CLOCK.fullmatch(_text(value))
    if match is None:
        raise _Invalid('must be a time "HH:MM" from 00:00 to 23:59')
    return time(int(match[1]), int(match[2]))


def _web_token(value: Any) -> str:
    token = _text(value)
    if token == PLACEHOLDER_TOKEN:
        raise _Invalid(f"is still the placeholder; set a random token of at least {MIN_TOKEN_LENGTH} characters")
    if len(token) < MIN_TOKEN_LENGTH:
        raise _Invalid(f"must have at least {MIN_TOKEN_LENGTH} characters")
    return token


def _client_id(value: Any) -> int:
    if _integer(value) == IBGUARD_CLIENT_ID:
        raise _Invalid("must differ from ibguard's client id")
    return value


_SECTIONS: dict[str, tuple[type, dict[str, Callable[[Any], Any]]]] = {
    "web": (WebConfig, {"port": _port, "token": _web_token}),
    "tiingo": (TiingoConfig, {"api_key": _text}),
    "finmind": (FinMindConfig, {"token": _text}),
    "fred": (FredConfig, {"api_key": _text}),
    "spylead": (ServiceConfig, {"url": _text, "token": _text}),
    "ibguard": (ServiceConfig, {"url": _text, "token": _text}),
    "ib": (IbConfig, {"host": _text, "port": _port, "client_id": _client_id, "account": _text}),
    "minute": (MinuteConfig, {"backfill_years": _positive_integer}),
    "orders": (OrdersConfig, {
        "us_order_kind": _choice(*US.order_kinds),
        "us_limit_offset_pct": _offset_pct,
        "tw_limit_offset_pct": _offset_pct,
    }),
    "limits": (Limits, {
        "us_max_orders_per_day": _positive_integer,
        "tw_max_orders_per_day": _positive_integer,
        "max_order_pct_of_equity": _positive_number,
        "us_max_order_value": _positive_number,
        "tw_max_order_value": _positive_number,
    }),
    "schedule": (ScheduleConfig, {"enabled": _flag, "us_update": _clock, "tw_update": _clock}),
    "email": (EmailConfig, {
        "smtp_host": _text,
        "smtp_port": _port,
        "username": _text,
        "password": _text,
        "sender": _text,
        "recipient": _text,
        "language": _choice(*LANGUAGES),
    }),
}
