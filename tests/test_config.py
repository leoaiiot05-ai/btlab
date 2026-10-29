"""config.toml: loading, validation and warnings (SPEC.md §22, AC-1)."""

import copy
import json
import math
import re
from datetime import date, time
from pathlib import Path

import pytest

from btlab.config import (
    ConfigError,
    EmailConfig,
    FinMindConfig,
    FredConfig,
    IbConfig,
    Limits,
    MinuteConfig,
    OrdersConfig,
    ScheduleConfig,
    ServiceConfig,
    TiingoConfig,
    WebConfig,
    load_config,
    startup_warnings,
)

REPO = Path(__file__).resolve().parents[1]
PLACEHOLDER = "web.token is still the placeholder; set a random token of at least 16 characters"

# Free text contains "sentinel" and numbers are unusual, so a test can tell whether a message quotes a value.
VALID = {
    "mode": "test",
    "web": {"port": 18091, "token": "Sentinel-Web-Token-4f9a2c"},
    "tiingo": {"api_key": "Sentinel-Tiingo-Key-7d1e"},
    "finmind": {"token": "Sentinel-FinMind-Token-3b8c"},
    "fred": {"api_key": "Sentinel-Fred-Key-9a6f"},
    "spylead": {"url": "http://sentinel-spylead.invalid:18090", "token": "Sentinel-Spylead-Token-2e5d"},
    "ibguard": {"url": "http://sentinel-ibguard.invalid:18080", "token": "Sentinel-Ibguard-Token-8c4b"},
    "ib": {"host": "sentinel-ib.invalid", "port": 14002, "client_id": 3131, "account": "Sentinel-DU7777"},
    "minute": {"backfill_years": 2},
    "orders": {"us_order_kind": "LIMIT", "us_limit_offset_pct": 1.25, "tw_limit_offset_pct": 3.75},
    "schedule": {"enabled": False, "us_update": "08:35", "tw_update": "18:05"},
    "email": {
        "smtp_host": "sentinel-smtp.invalid",
        "smtp_port": 10465,
        "username": "sentinel-user",
        "password": "Sentinel-Smtp-Password-6a1f",
        "sender": "sentinel-sender@example.invalid",
        "recipient": "sentinel-recipient@example.invalid",
        "language": "en",
    },
}
VALID_NUMBERS = ["18091", "18090", "18080", "14002", "3131", "10465", "1.25", "3.75", "08:35", "18:05"]
LIMITS = {
    "us_max_orders_per_day": 11,
    "tw_max_orders_per_day": 13,
    "max_order_pct_of_equity": 12.5,
    "us_max_order_value": 23456.5,
    "tw_max_order_value": 765432.5,
}
SECRETS = [
    VALID["web"]["token"],
    VALID["tiingo"]["api_key"],
    VALID["finmind"]["token"],
    VALID["fred"]["api_key"],
    VALID["spylead"]["token"],
    VALID["ibguard"]["token"],
    VALID["email"]["password"],
]
KEYS = [("tiingo", "api_key"), ("finmind", "token"), ("fred", "api_key"), ("spylead", "token"), ("ibguard", "token")]


def toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {toml(item)}" for key, item in value.items()) + "}"
    return repr(value)


def render(doc: dict) -> str:
    lines = [f"{key} = {toml(value)}" for key, value in doc.items() if not isinstance(value, dict)]
    for name, table in doc.items():
        if isinstance(table, dict):
            lines.append(f"[{name}]")
            lines += [f"{key} = {toml(value)}" for key, value in table.items()]
    return "\n".join(lines) + "\n"


def valid() -> dict:
    return copy.deepcopy(VALID)


def edited(section: str | None, key: str, value: object) -> dict:
    doc = valid()
    (doc if section is None else doc[section])[key] = value
    return doc


def without(section: str, key: str) -> dict:
    doc = valid()
    del doc[section][key]
    return doc


def trade(limits: dict | None = None) -> dict:
    doc = edited(None, "mode", "trade")
    if limits is not None:
        doc["limits"] = limits
    return doc


def write(tmp_path: Path, content: dict | str | bytes) -> Path:
    if isinstance(content, dict):
        content = render(content)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path = tmp_path / "config.toml"
    path.write_bytes(content)
    return path


def problems(path: Path) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    return caught.value.problems


def test_a_valid_file_loads(tmp_path):
    path = write(tmp_path, valid())
    config = load_config(path)
    assert config.path == path.absolute()
    assert config.root == path.absolute().parent
    assert config.mode == "test"
    assert config.web == WebConfig(**VALID["web"])
    assert config.tiingo == TiingoConfig(**VALID["tiingo"])
    assert config.finmind == FinMindConfig(**VALID["finmind"])
    assert config.fred == FredConfig(**VALID["fred"])
    assert config.spylead == ServiceConfig(**VALID["spylead"])
    assert config.ibguard == ServiceConfig(**VALID["ibguard"])
    assert config.ib == IbConfig(**VALID["ib"])
    assert config.minute == MinuteConfig(2)
    assert config.orders == OrdersConfig("LIMIT", 1.25, 3.75)
    assert config.schedule == ScheduleConfig(False, time(8, 35), time(18, 5))
    assert config.email == EmailConfig(**VALID["email"])
    assert config.limits is None
    assert startup_warnings(config) == []


def test_trade_mode_reads_its_limits_and_numbers_accept_integers(tmp_path):
    doc = trade(dict(LIMITS, max_order_pct_of_equity=10))
    doc["orders"]["us_limit_offset_pct"] = 2
    config = load_config(write(tmp_path, doc))
    assert config.mode == "trade"
    assert config.limits == Limits(11, 13, 10.0, 23456.5, 765432.5)
    assert isinstance(config.limits.max_order_pct_of_equity, float)
    assert isinstance(config.orders.us_limit_offset_pct, float)


def test_the_example_config_fails_only_on_the_placeholder_token():
    assert problems(REPO / "config.example.toml") == (PLACEHOLDER,)


def test_the_example_config_loads_once_a_token_is_set(tmp_path):
    text = (REPO / "config.example.toml").read_text(encoding="utf-8")
    config = load_config(write(tmp_path, text.replace('"change-me"', '"a-random-token-for-this-test"')))
    assert (config.mode, config.limits) == ("test", None)
    assert (config.web.port, config.ib.port, config.ib.client_id) == (8091, 4002, 31)
    assert config.schedule == ScheduleConfig(True, time(8, 30), time(18, 0))
    assert len(startup_warnings(config)) == len(KEYS)


def test_a_missing_file_is_an_error(tmp_path):
    assert problems(tmp_path / "config.toml") == ("the file does not exist",)


def test_unknown_keys_are_named(tmp_path):
    doc = edited("web", "tokn", "sentinel-typo")
    doc["debug"] = True
    doc["extras"] = {"note": "sentinel-extra"}
    assert set(problems(write(tmp_path, doc))) == {"unknown key debug", "unknown key extras", "unknown key web.tokn"}


def test_missing_keys_and_sections_are_named(tmp_path):
    doc = without("ib", "client_id")
    del doc["mode"]
    del doc["email"]
    assert set(problems(write(tmp_path, doc))) == {"mode is missing", "ib.client_id is missing", "section [email] is missing"}


@pytest.mark.parametrize(
    "section, key, value, problem",
    [
        (None, "mode", 1, "mode must be a string, not an integer"),
        (None, "web", 5, "web must be a table, not an integer"),
        ("web", "port", "8091", "web.port must be an integer, not a string"),
        ("web", "port", 8091.0, "web.port must be an integer, not a number"),
        ("ib", "client_id", True, "ib.client_id must be an integer, not a boolean"),
        ("tiingo", "api_key", 12345, "tiingo.api_key must be a string, not an integer"),
        ("web", "token", ["a-list-is-not-a-token"], "web.token must be a string, not an array"),
        ("ib", "host", {"name": "x"}, "ib.host must be a string, not a table"),
        ("schedule", "enabled", "true", "schedule.enabled must be true or false, not a string"),
        ("schedule", "us_update", time(8, 30), "schedule.us_update must be a string, not a date or time"),
        ("orders", "us_limit_offset_pct", "1.0", "orders.us_limit_offset_pct must be a number, not a string"),
        ("orders", "tw_limit_offset_pct", False, "orders.tw_limit_offset_pct must be a number, not a boolean"),
    ],
)
def test_wrong_types_are_named(tmp_path, section, key, value, problem):
    assert problems(write(tmp_path, edited(section, key, value))) == (problem,)


def test_the_placeholder_token_is_rejected(tmp_path):
    assert problems(write(tmp_path, edited("web", "token", "change-me"))) == (PLACEHOLDER,)


@pytest.mark.parametrize("token", ["", "fifteen-chars-x"])
def test_a_short_token_is_rejected(tmp_path, token):
    assert problems(write(tmp_path, edited("web", "token", token))) == ("web.token must have at least 16 characters",)


def test_a_token_of_16_characters_is_enough(tmp_path):
    assert load_config(write(tmp_path, edited("web", "token", "sixteen-chars-xy"))).web.token == "sixteen-chars-xy"


def test_ibguard_client_id_is_rejected(tmp_path):
    assert problems(write(tmp_path, edited("ib", "client_id", 17))) == ("ib.client_id must differ from ibguard's client id",)


def test_trade_mode_without_limits_names_every_limit(tmp_path):
    (problem,) = problems(write(tmp_path, trade()))
    assert problem.startswith("trade mode needs [limits] with ")
    assert problem.endswith(" above 0")
    assert all(key in problem for key in LIMITS)


def test_trade_mode_with_a_limit_missing_names_it(tmp_path):
    limits = dict(LIMITS)
    del limits["tw_max_order_value"]
    assert problems(write(tmp_path, trade(limits))) == ("limits.tw_max_order_value is missing",)


@pytest.mark.parametrize(
    "key, value",
    [
        ("us_max_orders_per_day", 0),
        ("tw_max_orders_per_day", -2),
        ("max_order_pct_of_equity", 0.0),
        ("us_max_order_value", -100.5),
        ("tw_max_order_value", 0),
    ],
)
def test_trade_mode_with_a_limit_not_above_zero_names_it(tmp_path, key, value):
    assert problems(write(tmp_path, trade(dict(LIMITS, **{key: value})))) == (f"limits.{key} must be above 0",)


def test_limits_are_checked_in_test_mode_too(tmp_path):
    doc = edited(None, "limits", dict(LIMITS, us_max_orders_per_day=2.5))
    assert problems(write(tmp_path, doc)) == ("limits.us_max_orders_per_day must be an integer, not a number",)


@pytest.mark.parametrize(
    "section, key, value, problem",
    [
        (None, "mode", "live", 'mode must be "test" or "trade"'),
        ("web", "port", 0, "web.port must be a port number from 1 to 65535"),
        ("ib", "port", 65536, "ib.port must be a port number from 1 to 65535"),
        ("email", "smtp_port", -1, "email.smtp_port must be a port number from 1 to 65535"),
        ("minute", "backfill_years", 0, "minute.backfill_years must be above 0"),
        ("orders", "us_order_kind", "MKT", 'orders.us_order_kind must be "MOO" or "LIMIT"'),
        ("orders", "us_limit_offset_pct", -0.5, "orders.us_limit_offset_pct must be at least 0 and below 100"),
        ("orders", "tw_limit_offset_pct", 100, "orders.tw_limit_offset_pct must be at least 0 and below 100"),
        ("orders", "tw_limit_offset_pct", math.nan, "orders.tw_limit_offset_pct must be a finite number"),
        ("orders", "us_limit_offset_pct", math.inf, "orders.us_limit_offset_pct must be a finite number"),
        ("schedule", "us_update", "8:30", 'schedule.us_update must be a time "HH:MM" from 00:00 to 23:59'),
        ("schedule", "tw_update", "24:00", 'schedule.tw_update must be a time "HH:MM" from 00:00 to 23:59'),
        ("email", "language", "fr", 'email.language must be "zh-TW" or "en"'),
    ],
)
def test_values_outside_their_rules_are_named(tmp_path, section, key, value, problem):
    assert problems(write(tmp_path, edited(section, key, value))) == (problem,)


def test_times_cover_the_whole_day(tmp_path):
    doc = valid()
    doc["schedule"].update(us_update="00:00", tw_update="23:59")
    config = load_config(write(tmp_path, doc))
    assert (config.schedule.us_update, config.schedule.tw_update) == (time(0, 0), time(23, 59))


def test_empty_keys_load_with_a_warning_each(tmp_path):
    doc = valid()
    for section, key in KEYS:
        doc[section][key] = ""
    warnings = startup_warnings(load_config(write(tmp_path, doc)))
    assert warnings == [f"{section}.{key} is empty; its requests will fail with MISSING_KEY" for section, key in KEYS]


def test_a_disabled_service_needs_no_token(tmp_path):
    doc = valid()
    doc["spylead"] = {"url": "", "token": ""}
    doc["ibguard"] = {"url": "", "token": ""}
    assert startup_warnings(load_config(write(tmp_path, doc))) == []


def test_printing_a_config_shows_no_secret(tmp_path):
    text = repr(load_config(write(tmp_path, valid())))
    assert "18091" in text
    assert not [secret for secret in SECRETS if secret in text]


def test_a_byte_order_mark_is_accepted(tmp_path):
    assert load_config(write(tmp_path, b"\xef\xbb\xbf" + render(valid()).encode("utf-8"))).mode == "test"


def test_a_syntax_error_gives_its_position(tmp_path):
    text = render(valid()).replace('"Sentinel-Web-Token-4f9a2c"', '"Sentinel-Web-Token-4f9a2c')
    (problem,) = problems(write(tmp_path, text))
    assert re.fullmatch(r"is not valid TOML \(line 4, column \d+\)", problem)


ECHO_CASES = {
    "unknown key": (lambda: edited("web", "tokn", "sentinel-typo"), None),
    "unknown section": (lambda: edited(None, "extras", {"note": "sentinel-extra"}), None),
    "missing key": (lambda: without("web", "token"), None),
    "string for an integer": (lambda: edited("web", "port", "sentinel-port"), None),
    "number for a string": (lambda: edited("tiingo", "api_key", 98765.4321), "98765"),
    "table for a string": (lambda: edited("fred", "api_key", {"key": "sentinel-nested"}), None),
    "array for a string": (lambda: edited("finmind", "token", ["sentinel-in-array"]), None),
    "section not a table": (lambda: edited(None, "web", "sentinel-web"), None),
    "placeholder token": (lambda: edited("web", "token", "change-me"), "change-me"),
    "short token": (lambda: edited("web", "token", "sentinel-short"), None),
    "ibguard client id": (lambda: edited("ib", "client_id", 17), "17"),
    "port out of range": (lambda: edited("ib", "port", 99999), "99999"),
    "unknown mode": (lambda: edited(None, "mode", "sentinel-mode"), None),
    "unknown language": (lambda: edited("email", "language", "sentinel-language"), None),
    "bad time": (lambda: edited("schedule", "us_update", "sentinel-time"), None),
    "offset out of range": (lambda: edited("orders", "us_limit_offset_pct", 4321.5), "4321"),
    "trade mode without limits": (lambda: trade(), None),
    "negative limit": (lambda: trade(dict(LIMITS, us_max_order_value=-98765.25)), "98765"),
    "syntax error in a secret": (lambda: render(valid()).replace("4f9a2c\"", "4f9a2c"), None),
    "control character in a secret": (lambda: render(valid()).replace("Password-6a1f", "Password-\x07-6a1f"), "x07"),
    "invalid UTF-8 in a secret": (lambda: render(valid()).encode("utf-8").replace(b"Key-9a6f", b"Key-\xff"), "0xff"),
}


@pytest.mark.parametrize("build, offending", ECHO_CASES.values(), ids=ECHO_CASES.keys())
def test_no_message_quotes_a_value_of_the_file(tmp_path, build, offending):
    path = write(tmp_path, build())
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value).replace(str(path), "<path>").lower()
    assert caught.value.problems
    for fragment in ["sentinel", *VALID_NUMBERS, *([offending] if offending else [])]:
        assert fragment.lower() not in message
