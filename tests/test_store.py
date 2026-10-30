"""The store: data/btlab.db and data/minute.db (SPEC.md §18, AC-2)."""

import functools
import logging
import math
import re
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.records import (
    EVENT_KINDS,
    Action,
    Contract,
    DailyBars,
    Event,
    ExternalSeries,
    ExternalSnapshot,
    JobRun,
    MinuteBars,
    MinuteCoverage,
    RiskFreeRates,
    ShortSuspension,
    SymbolInfo,
    Trade,
)
from btlab.store import MinuteStore, Store, StoreError

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 29, 6, 3, 11, 123456, tzinfo=timezone.utc)
CONNECT = sqlite3.connect


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def store(folder):
    opened = Store(folder)
    yield opened
    opened.close()


@pytest.fixture
def minute_store(folder):
    opened = MinuteStore(folder)
    yield opened
    opened.close()


def raw(path: Path) -> sqlite3.Connection:
    return CONNECT(path, autocommit=True)


def bars(days, close, volume=None, no_trade=None) -> DailyBars:
    """Bars whose open, high and low are offsets of close, so a revision shows in every column."""
    close = np.asarray(close, dtype=np.float64)
    return DailyBars(
        day=np.asarray(days, dtype=np.int64),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.full(len(close), 1000.0) if volume is None else np.asarray(volume, dtype=np.float64),
        no_trade=np.zeros(len(close), dtype=bool) if no_trade is None else np.asarray(no_trade, dtype=bool),
    )


def assert_bars_equal(actual: DailyBars, expected: DailyBars) -> None:
    for field in ("day", "open", "high", "low", "close", "volume", "no_trade"):
        assert_array_equal(getattr(actual, field), getattr(expected, field), err_msg=field)
    assert actual.day.dtype == np.int64 and actual.no_trade.dtype == bool
    assert actual.close.dtype == actual.volume.dtype == np.float64


def minute_bars(t, close) -> MinuteBars:
    close = np.asarray(close, dtype=np.float64)
    return MinuteBars(np.asarray(t, dtype=np.int64), close - 0.1, close + 0.2, close - 0.2, close, np.full(len(close), 300.0))


# ----- files and schema -----


def spec_schema() -> dict[str, str]:
    """The SQL block of SPEC.md §18, split into its two files."""
    text = (REPO / "SPEC.md").read_text(encoding="utf-8")
    block = re.search(r"```sql\n(-- data/btlab\.db\n.*?)```", text, re.S)[1]
    btlab, minute = block.split("-- data/minute.db\n")
    return {"btlab.db": btlab, "minute.db": minute}


def schema_of(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    return {(kind, name): " ".join(re.sub(r"--[^\n]*", "", sql).split()) for kind, name, sql in rows}


def tables_of(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {name for (name,) in rows}


def test_both_files_hold_the_schema_of_section_18(folder):
    assert not folder.exists()
    Store(folder).close()
    MinuteStore(folder).close()
    for name, sql in spec_schema().items():
        expected = CONNECT(":memory:")
        expected.executescript(sql)
        actual = raw(folder / name)
        assert schema_of(actual) == schema_of(expected), name
        assert actual.execute("PRAGMA user_version").fetchone()[0] == 1
        actual.close()
        expected.close()
    btlab, minute = raw(folder / "btlab.db"), raw(folder / "minute.db")
    assert not {table for table in tables_of(btlab) if table.startswith("minute")}
    assert tables_of(minute) == {"minute_bars", "minute_coverage"}
    btlab.close()
    minute.close()


def put_symbol(store: Store) -> None:
    store.add_symbol("US", "SPY", "ETF", T0)


def got_symbol(store: Store) -> list[str]:
    return [info.symbol for info in store.symbols("US")]


def put_coverage(store: MinuteStore) -> None:
    store.save_minute_day("US", "SPY", MinuteCoverage(20000, "EMPTY", None, T0))


def got_coverage(store: MinuteStore) -> list[str]:
    return [coverage.status for coverage in store.minute_coverage("US", "SPY")]


FILES = [
    pytest.param(Store, put_symbol, got_symbol, ["SPY"], id="btlab.db"),
    pytest.param(MinuteStore, put_coverage, got_coverage, ["EMPTY"], id="minute.db"),
]


@pytest.mark.parametrize("cls, put, get, expected", FILES)
def test_schema_creation_is_idempotent(folder, cls, put, get, expected):
    first = cls(folder)
    schema = schema_of(first._db)
    put(first)
    first.close()
    second = cls(folder)
    assert schema_of(second._db) == schema
    assert get(second) == expected
    second.close()
    connection = raw(folder / cls.FILE_NAME)
    connection.execute("PRAGMA user_version = 0")
    connection.close()
    third = cls(folder)
    assert schema_of(third._db) == schema
    assert get(third) == expected
    assert third._db.execute("PRAGMA user_version").fetchone()[0] == 1
    third.close()


@pytest.mark.parametrize("cls", [Store, MinuteStore])
def test_a_newer_schema_version_is_refused(folder, cls):
    cls(folder).close()
    connection = raw(folder / cls.FILE_NAME)
    connection.execute("PRAGMA user_version = 2")
    connection.close()
    with pytest.raises(StoreError, match="schema version 2 is newer") as caught:
        cls(folder)
    assert cls.FILE_NAME in str(caught.value)
    (folder / cls.FILE_NAME).unlink()  # the failed open closed its connection


@pytest.mark.parametrize("cls", [Store, MinuteStore])
def test_a_file_that_is_not_sqlite_is_a_store_error(folder, cls):
    folder.mkdir()
    (folder / cls.FILE_NAME).write_bytes(b"this is not a database\n" * 200)
    with pytest.raises(StoreError, match="not a database"):
        cls(folder)
    (folder / cls.FILE_NAME).unlink()


@pytest.mark.parametrize("cls", [Store, MinuteStore])
def test_the_pragmas_of_section_18(folder, cls):
    opened = cls(folder)
    pragma = lambda name: opened._db.execute(f"PRAGMA {name}").fetchone()[0]  # noqa: E731
    assert pragma("journal_mode") == "wal"
    assert pragma("synchronous") == 1  # NORMAL
    assert pragma("busy_timeout") == 1000
    assert pragma("foreign_keys") == 1
    opened.close()


class WalAnswersDelete(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql == "PRAGMA journal_mode = WAL":
            return super().execute("PRAGMA journal_mode")
        return super().execute(sql, parameters)


class WalRaises(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql == "PRAGMA journal_mode = WAL":
            raise sqlite3.OperationalError("disk I/O error")
        return super().execute(sql, parameters)


class NoJournalModeChanges(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql.startswith("PRAGMA journal_mode = "):
            return super().execute("SELECT 'off'")
        return super().execute(sql, parameters)


@pytest.mark.parametrize("factory, reason", [(WalAnswersDelete, "stayed delete"), (WalRaises, "disk I/O error")])
@pytest.mark.parametrize("cls, put, get, expected", FILES)
def test_wal_falls_back_to_delete_with_a_warning(folder, monkeypatch, caplog, factory, reason, cls, put, get, expected):
    monkeypatch.setattr(sqlite3, "connect", functools.partial(CONNECT, factory=factory))
    with caplog.at_level(logging.WARNING, logger="btlab.store"):
        opened = cls(folder)
    pragma = lambda name: opened._db.execute(f"PRAGMA {name}").fetchone()[0]  # noqa: E731
    assert pragma("journal_mode") == "delete"
    assert pragma("synchronous") == 2  # FULL
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert str(opened.path) in record.getMessage() and reason in record.getMessage()
    put(opened)
    assert get(opened) == expected
    opened.close()


@pytest.mark.parametrize("cls", [Store, MinuteStore])
def test_opening_fails_when_no_journal_mode_can_be_set(folder, monkeypatch, cls):
    monkeypatch.setattr(sqlite3, "connect", functools.partial(CONNECT, factory=NoJournalModeChanges))
    with pytest.raises(StoreError, match="neither journal mode WAL nor DELETE"):
        cls(folder)
    (folder / cls.FILE_NAME).unlink()


@pytest.mark.parametrize("cls, put, get, expected", FILES)
def test_a_write_against_a_locked_database_fails_within_the_busy_timeout(folder, cls, put, get, expected):
    opened = cls(folder)
    holder = raw(folder / cls.FILE_NAME)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(StoreError, match="database is locked") as caught:
        put(opened)
    waited = time.monotonic() - started
    assert 0.9 <= waited < 5
    assert not caught.value.conflict
    holder.execute("ROLLBACK")
    holder.close()
    put(opened)
    assert get(opened) == expected
    opened.close()


def test_a_failed_write_changes_nothing(store):
    store.upsert_daily_bars("US", "SPY", bars([1, 2], [10.0, 11.0]))
    with pytest.raises(StoreError, match="NOT NULL constraint failed: daily_bars.volume"):
        store.upsert_daily_bars("US", "SPY", bars([2, 3, 4], [12.0, 13.0, 14.0], volume=[5.0, 5.0, math.nan]))
    assert_bars_equal(store.daily_bars("US", "SPY"), bars([1, 2], [10.0, 11.0]))


def test_times_are_utc_iso_8601(store):
    taipei = T0.astimezone(timezone(timedelta(hours=8)))
    store.add_symbol("TW", "2330", "STOCK", taipei)
    (text,) = store._db.execute("SELECT added_at FROM symbols").fetchone()
    assert text == "2026-10-29T06:03:11.123456Z"
    (info,) = store.symbols("TW")
    assert info.added_at == taipei and info.added_at.utcoffset() == timedelta(0)
    with pytest.raises(ValueError, match="aware"):
        store.add_symbol("TW", "2317", "STOCK", datetime(2026, 10, 29, 6, 3))


ISO = "2026-10-29T06:03:11.000000Z"
BACKTEST = {
    "id": 1, "created_at": ISO, "name": "MA cross", "market": "US", "timeframe": "1d", "config": '{"market": "US"}',
    "data_stamp": '{"SPY": [20000, 20000]}', "metrics": '{"cagr": 0.1}', "tables": '{"annual": []}',
    "flags": '["SURVIVORSHIP"]', "keep_offline": 1, "opened_at": ISO,
}
PORTFOLIO = {
    "id": 1, "market": "US", "mode": "test", "backtest_id": 1, "config": '{"market": "US"}', "capital": 100000.0,
    "created_at": ISO, "first_day": 20000, "active": 1,
}
BATCH = {
    "id": 1, "portfolio_id": 1, "session_day": 20001, "created_at": ISO, "status": "CONFIRMED",
    "confirmed_at": ISO, "positions_seen": '{"SPY": 10}',
}
ORDER = {
    "id": 1, "batch_id": 1, "line": 1, "symbol": "SPY", "con_id": 756733, "action": "SELL", "quantity": 10.0,
    "kind": "LIMIT", "limit_price": 571.25, "ref_price": 577.0, "status": "FILLED", "reason": None,
    "order_ref": "btlab:1:1:1761717791000", "ib_order_id": 42, "filled_qty": 10.0, "avg_price": 571.5,
    "commission": 1.0, "updated_at": ISO,
}
# Each case inserts its parents first; the last row is the one that must round-trip.
RAW_ROWS = {
    "rules": [("rules", {"id": 1, "name": "breakout", "body": '{"version": 1}', "updated_at": ISO})],
    "optimizations": [("optimizations", {
        "id": 1, "created_at": ISO, "name": "grid", "market": "TW", "mode": "WFO", "status": "DONE",
        "config": '{"market": "TW"}', "results": "[]", "best": '{"fast": 20}', "flags": "[]",
    })],
    "live_portfolios": [("backtests", BACKTEST), ("live_portfolios", PORTFOLIO)],
    "live_batches": [("backtests", BACKTEST), ("live_portfolios", PORTFOLIO), ("live_batches", BATCH)],
    "live_orders": [("backtests", BACKTEST), ("live_portfolios", PORTFOLIO), ("live_batches", BATCH), ("live_orders", ORDER)],
    "live_holdings": [("live_holdings", {"portfolio_id": 1, "symbol": "SPY", "quantity": 10.0})],
    "live_ledger": [("live_ledger", {"portfolio_id": 1, "day": 20001, "cash": 94000.5, "equity": 100100.0, "holdings": '{"SPY": 10}'})],
    "ibguard_exclusions": [
        ("backtests", BACKTEST), ("live_portfolios", PORTFOLIO), ("live_batches", BATCH), ("live_orders", ORDER),
        ("ibguard_exclusions", {
            "id": 1, "order_id": 1, "con_id": 756733, "symbol": "SPY", "state": "RESTORED", "excluded_at": ISO,
            "resolved_at": ISO,
        }),
    ],
}
# The tables whose rows round-trip through access functions below.
ACCESS_TABLES = {
    "symbols", "lists", "list_members", "daily_bars", "actions", "trading_days", "short_suspensions", "rf_rates",
    "external_series", "contracts", "job_runs", "settings", "events", "backtests", "backtest_series", "trades",
}


def test_every_table_of_btlab_db_has_a_round_trip(store):
    assert tables_of(store._db) == ACCESS_TABLES | RAW_ROWS.keys()


@pytest.mark.parametrize("table", sorted(RAW_ROWS))
def test_tables_without_access_functions_round_trip(folder, table):
    Store(folder).close()
    connection = raw(folder / "btlab.db")
    connection.execute("PRAGMA foreign_keys = ON")
    for name, row in RAW_ROWS[table]:
        columns = ", ".join(row)
        connection.execute(f"INSERT INTO {name} ({columns}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    row = RAW_ROWS[table][-1][1]
    assert connection.execute(f"SELECT {', '.join(row)} FROM {table}").fetchall() == [tuple(row.values())]
    connection.close()


# ----- symbols and lists (§7) -----


def test_symbols_round_trip_with_every_setter(store):
    assert store.add_symbol("TW", "2330", "STOCK", at(0))
    assert store.add_symbol("TW", "00679B", "BOND_ETF", at(1))
    assert store.add_symbol("US", "SPY", "STOCK", at(2))
    assert not store.add_symbol("TW", "2330", "ETF", at(3))
    assert store.set_symbol_details("TW", "2330", "台積電", "TWSE")
    assert store.set_category("US", "SPY", "ETF")
    assert store.set_minute_enabled("TW", "2330", True)
    assert store.set_borrow_fee("US", "SPY", 1.25)
    assert store.record_fetch("TW", "2330", at(5), "HTTP_500 FinMind answered 500")
    assert store.symbols("TW") == [
        SymbolInfo("TW", "00679B", None, "BOND_ETF", "RULE", None, False, None, None, None, at(1)),
        SymbolInfo("TW", "2330", "台積電", "STOCK", "RULE", "TWSE", True, None, at(5), "HTTP_500 FinMind answered 500", at(0)),
    ]
    assert store.symbols("US") == [SymbolInfo("US", "SPY", None, "ETF", "OWNER", None, False, 1.25, None, None, at(2))]
    assert store.record_fetch("TW", "2330", at(6), None)
    assert store.set_borrow_fee("US", "SPY", None)
    assert store.set_minute_enabled("TW", "2330", False)
    (tsmc,) = [info for info in store.symbols("TW") if info.symbol == "2330"]
    assert (tsmc.last_fetch_at, tsmc.last_fetch_error, tsmc.minute_enabled) == (at(6), None, False)
    assert store.symbols("US")[0].borrow_fee_pct is None


def test_symbol_setters_report_unknown_symbols(store):
    store.add_symbol("US", "SPY", "ETF", T0)
    assert not store.set_symbol_details("US", "QQQ", "Invesco QQQ", None)
    assert not store.set_category("US", "QQQ", "ETF")
    assert not store.set_minute_enabled("TW", "SPY", True)
    assert not store.set_borrow_fee("US", "QQQ", 0.5)
    assert not store.record_fetch("US", "QQQ", T0, None)
    assert not store.purge_symbol("US", "QQQ")
    assert [info.symbol for info in store.symbols("US")] == ["SPY"]


@pytest.mark.parametrize(
    "call",
    [
        lambda store: store.add_symbol("US", "TSM", "TDR", T0),
        lambda store: store.add_symbol("JP", "7203", "STOCK", T0),
        lambda store: store.set_category("US", "SPY", "BOND_ETF"),
        lambda store: store.set_borrow_fee("US", "SPY", -0.5),
        lambda store: store.set_borrow_fee("US", "SPY", math.nan),
    ],
)
def test_invalid_symbol_values_are_rejected(store, call):
    store.add_symbol("US", "SPY", "ETF", T0)
    with pytest.raises(ValueError):
        call(store)
    assert store.symbols("US") == [SymbolInfo("US", "SPY", None, "ETF", "RULE", None, False, None, None, None, T0)]


def test_lists_keep_their_order_and_share_symbols(store):
    tech = store.create_list("tech", "US", at(0))
    taiwan = store.create_list("台灣", "TW", at(1))
    etfs = store.create_list("etfs", "US", at(2))
    assert store.set_list_symbols(tech, ["QQQ", "AAPL", "MSFT"])
    assert store.set_list_symbols(taiwan, ["2330", "0050"])
    assert store.set_list_symbols(etfs, ["SPY", "QQQ"])
    assert [(l.id, l.name, l.market, l.created_at, l.symbols) for l in store.lists()] == [
        (tech, "tech", "US", at(0), ("QQQ", "AAPL", "MSFT")),
        (taiwan, "台灣", "TW", at(1), ("2330", "0050")),
        (etfs, "etfs", "US", at(2), ("SPY", "QQQ")),
    ]
    assert [l.name for l in store.lists("US")] == ["tech", "etfs"]
    assert store.rename_list(tech, "big tech")
    assert store.set_list_symbols(tech, ["MSFT", "AAPL"])
    assert store.set_list_symbols(taiwan, [])
    by_id = {l.id: l for l in store.lists()}
    assert (by_id[tech].name, by_id[tech].symbols, by_id[taiwan].symbols) == ("big tech", ("MSFT", "AAPL"), ())


def test_deleting_a_list_deletes_its_members(store):
    first = store.create_list("first", "US", T0)
    second = store.create_list("second", "US", T0)
    store.set_list_symbols(first, ["SPY", "QQQ"])
    store.set_list_symbols(second, ["SPY"])
    assert store.delete_list(first)
    assert [(l.name, l.symbols) for l in store.lists()] == [("second", ("SPY",))]
    assert store._db.execute("SELECT list_id, symbol FROM list_members").fetchall() == [(second, "SPY")]


def test_list_names_are_unique_and_symbols_appear_once(store):
    first = store.create_list("mine", "US", T0)
    second = store.create_list("other", "US", T0)
    with pytest.raises(StoreError) as caught:
        store.create_list("mine", "TW", T0)
    assert caught.value.conflict
    with pytest.raises(StoreError) as caught:
        store.rename_list(second, "mine")
    assert caught.value.conflict
    with pytest.raises(ValueError):
        store.set_list_symbols(first, ["SPY", "QQQ", "SPY"])
    with pytest.raises(ValueError):
        store.create_list("japan", "JP", T0)
    assert [(l.name, l.symbols) for l in store.lists()] == [("mine", ()), ("other", ())]


def test_rules_are_stored_by_unique_name(store):
    breakout = store.create_rule("breakout", {"version": 1, "side": "long"}, T0)
    fade = store.create_rule("fade", {"version": 1, "side": "short"}, T0)
    with pytest.raises(StoreError) as caught:
        store.create_rule("fade", {"version": 1}, T0)
    assert caught.value.conflict
    assert store.update_rule(breakout, "alpha", {"version": 1, "side": "short"}, at(5))
    with pytest.raises(StoreError) as caught:
        store.update_rule(breakout, "fade", {"version": 1}, T0)
    assert caught.value.conflict
    assert [(r.id, r.name, r.rule, r.updated_at) for r in store.rules()] == [
        (breakout, "alpha", {"version": 1, "side": "short"}, at(5)), (fade, "fade", {"version": 1, "side": "short"}, T0)
    ]
    assert store.delete_rule(fade) and not store.delete_rule(fade)
    assert not store.update_rule(fade, "gone", {}, T0)
    assert [r.name for r in store.rules()] == ["alpha"]


def test_unknown_lists_are_reported(store):
    assert not store.rename_list(7, "x")
    assert not store.set_list_symbols(7, ["SPY"])
    assert not store.delete_list(7)
    assert store.lists() == []
    assert store._db.execute("SELECT COUNT(*) FROM list_members").fetchone()[0] == 0


# ----- daily bars, actions, calendars, rates and contracts -----


def test_daily_bars_round_trip_with_a_no_trade_day(store):
    stored = bars([100, 101, 102, 105], [50.0, math.nan, 51.5, 52.0], volume=[1000.0, 250.0, 0.0, 3000.0],
                  no_trade=[False, True, False, False])
    stored.open[1] = stored.high[1] = stored.low[1] = math.nan
    assert store.upsert_daily_bars("TW", "2330", stored) == 4
    assert_bars_equal(store.daily_bars("TW", "2330"), stored)
    window = store.daily_bars("TW", "2330", first_day=101, last_day=102)
    assert_array_equal(window.day, [101, 102])
    assert_array_equal(store.daily_bars("TW", "2330", first_day=102).day, [102, 105])
    assert_array_equal(store.daily_bars("TW", "2330", last_day=101).day, [100, 101])
    assert store.daily_bar_range("TW", "2330") == (100, 105)
    assert store.daily_bar_range("TW", "2317") is None
    assert store.daily_bar_range("US", "2330") is None


def test_an_empty_read_has_the_right_dtypes(store):
    empty = store.daily_bars("US", "SPY")
    assert len(empty.day) == 0
    assert_bars_equal(empty, bars([], []))


def test_arrays_of_different_lengths_are_rejected(store):
    broken = bars([1, 2, 3], [1.0, 2.0, 3.0])
    broken = DailyBars(broken.day, broken.open, broken.high, broken.low, broken.close[:2], broken.volume, broken.no_trade)
    with pytest.raises(ValueError):
        store.upsert_daily_bars("US", "SPY", broken)
    with pytest.raises(ValueError):
        store.upsert_daily_bars("JP", "7203", bars([1], [1.0]))
    assert store.daily_bar_range("US", "SPY") is None


def test_upserting_an_overlap_window_replaces_only_its_days(store):
    first = bars(range(1, 11), [100.0 + day / 7 for day in range(1, 11)])
    store.upsert_daily_bars("US", "SPY", first)
    store.upsert_daily_bars("US", "QQQ", first)
    revised = bars(range(8, 13), [first.close[7], 222.0, 333.0, 444.0, 555.0])
    assert store.upsert_daily_bars("US", "SPY", revised) == 4
    after = store.daily_bars("US", "SPY")
    assert_array_equal(after.day, range(1, 13))
    assert after.close[:8].tobytes() == first.close[:8].tobytes()
    assert after.open[:8].tobytes() == first.open[:8].tobytes()
    assert_array_equal(after.close[7:], revised.close)
    assert_array_equal(after.high[7:], revised.high)
    assert store.upsert_daily_bars("US", "SPY", revised) == 0
    assert_bars_equal(store.daily_bars("US", "QQQ"), first)


def test_a_no_trade_flag_or_a_null_price_is_a_change(store):
    store.upsert_daily_bars("TW", "2330", bars([1, 2], [10.0, 11.0]))
    flagged = bars([2], [math.nan], volume=[1000.0], no_trade=[True])
    assert store.upsert_daily_bars("TW", "2330", flagged) == 1
    assert store.upsert_daily_bars("TW", "2330", flagged) == 0


DIVIDEND = Action(200, "CASH_DIV", "DIV_RESULT", cash=2.5, ref_before=248.5, ref_after=240.5)
SPLIT = Action(200, "SHARE_RATIO", "STOCK_DIV", ratio=1.1)
REDUCTION = Action(250, "SHARE_RATIO", "CAPITAL_REDUCTION", ratio=0.75)
LATE = Action(400, "CASH_DIV", "DIV_RESULT", cash=3.0)


def test_actions_round_trip_with_every_field(store):
    assert store.replace_actions("TW", "2330", 100, 300, [REDUCTION, SPLIT, DIVIDEND]) == 3
    assert store.actions("TW", "2330") == [DIVIDEND, SPLIT, REDUCTION]
    assert store.actions("TW", "2330", first_day=201) == [REDUCTION]
    assert store.actions("TW", "2330", last_day=200) == [DIVIDEND, SPLIT]
    assert store.actions("US", "2330") == []


def test_replacing_actions_touches_only_the_window(store):
    store.replace_actions("TW", "2330", 100, 300, [DIVIDEND, SPLIT, REDUCTION])
    store.replace_actions("TW", "2330", 301, 500, [LATE])
    corrected = Action(200, "CASH_DIV", "DIV_RESULT", cash=2.75, ref_before=248.5, ref_after=240.25)
    added = Action(260, "CASH_DIV", "DIV_RESULT", cash=1.0)
    # The dividend changes, the stock dividend is withdrawn and a new dividend appears; REDUCTION stays.
    assert store.replace_actions("TW", "2330", 150, 350, [corrected, REDUCTION, added]) == 3
    assert store.actions("TW", "2330") == [corrected, REDUCTION, added, LATE]
    assert store.replace_actions("TW", "2330", 150, 350, [added, REDUCTION, corrected]) == 0
    assert store.replace_actions("TW", "2330", 150, 350, []) == 3
    assert store.actions("TW", "2330") == [LATE]


@pytest.mark.parametrize(
    "actions",
    [
        [DIVIDEND, Action(99, "CASH_DIV", "DIV_RESULT", cash=1.0)],
        [Action(200, "STOCK_DIV", "STOCK_DIV", ratio=1.1)],
        [DIVIDEND, Action(200, "CASH_DIV", "DIV_RESULT", cash=9.0)],
    ],
    ids=["outside the window", "unknown kind", "a key twice"],
)
def test_invalid_actions_change_nothing(store, actions):
    store.replace_actions("TW", "2330", 100, 300, [SPLIT])
    with pytest.raises(ValueError):
        store.replace_actions("TW", "2330", 100, 300, actions)
    assert store.actions("TW", "2330") == [SPLIT]


def test_trading_days_replace_the_whole_calendar(store):
    assert store.replace_trading_days("TW", [5, 3, 4, 8]) == 4
    assert_array_equal(store.trading_days("TW"), [3, 4, 5, 8])
    assert store.trading_days("TW").dtype == np.int64
    assert store.replace_trading_days("TW", np.array([3, 4, 5, 8, 9])) == 1
    assert store.replace_trading_days("TW", [4, 5, 8, 9, 10]) == 2
    assert_array_equal(store.trading_days("TW"), [4, 5, 8, 9, 10])
    assert store.replace_trading_days("TW", [4, 5, 8, 9, 10]) == 0
    assert store.trading_days("US").dtype == np.int64 and len(store.trading_days("US")) == 0


def test_short_suspensions_replace_their_window(store):
    early = ShortSuspension(100, 104, "股東常會")
    middle = ShortSuspension(200, 203, "除權息")
    late = ShortSuspension(300, 302, None)
    assert store.replace_short_suspensions("2330", 0, 400, [late, early, middle]) == 3
    assert store.short_suspensions("2330") == [early, middle, late]
    moved = ShortSuspension(200, 206, "除權息")
    new = ShortSuspension(250, 251, "現金增資")
    assert store.replace_short_suspensions("2330", 150, 290, [new, moved]) == 2
    assert store.short_suspensions("2330") == [early, moved, new, late]
    assert store.replace_short_suspensions("2330", 150, 290, [moved, new]) == 0
    with pytest.raises(ValueError):
        store.replace_short_suspensions("2330", 150, 290, [early])
    assert store.short_suspensions("2317") == []


def test_risk_free_rates_round_trip(store):
    rates = RiskFreeRates(np.array([10, 11, 14]), np.array([4.25, 4.3, 4.31]))
    assert store.upsert_rf_rates(rates) == 3
    assert store.upsert_rf_rates(RiskFreeRates(np.array([14, 15]), np.array([4.29, 4.4]))) == 2
    assert store.upsert_rf_rates(RiskFreeRates(np.array([14, 15]), np.array([4.29, 4.4]))) == 0
    read = store.rf_rates()
    assert_array_equal(read.day, [10, 11, 14, 15])
    assert_array_equal(read.rate_pct, [4.25, 4.3, 4.29, 4.4])
    assert read.day.dtype == np.int64
    assert_array_equal(store.rf_rates(first_day=11, last_day=14).day, [11, 14])


def test_contracts_are_saved_and_replaced(store):
    first = Contract(265598, "NASDAQ", "USD", 1.0, 1.0, "26,26,26", T0)
    assert store.contract("US", "AAPL") is None
    store.save_contract("US", "AAPL", first)
    assert store.contract("US", "AAPL") == first
    second = Contract(265598, None, "USD", None, None, None, at(60))
    store.save_contract("US", "AAPL", second)
    assert store.contract("US", "AAPL") == second
    assert store.contract("TW", "AAPL") is None


# ----- external series (§5.6) -----


def test_external_series_round_trip_as_float64_little_endian(store):
    values = [math.nan, math.inf, -math.inf, -0.0, 5e-324, 1.5, -2.25]
    big_endian = np.array(values, dtype=">f8")
    store.replace_external_series(
        ExternalSnapshot(T0, (ExternalSeries("SPYL1", 9000, big_endian, False), ExternalSeries("F", 9100, np.array([0.5]), True)))
    )
    snapshot = store.external_snapshot()
    assert snapshot.taken_at == T0
    f, spyl1 = snapshot.series
    assert (f.name, f.first_day, f.in_sample, spyl1.name, spyl1.first_day, spyl1.in_sample) == ("F", 9100, True, "SPYL1", 9000, False)
    assert spyl1.values.dtype == np.float64 and spyl1.values.dtype.isnative
    assert spyl1.values.tobytes() == np.array(values, dtype=np.float64).tobytes()
    blob = store._db.execute("SELECT values_blob FROM external_series WHERE name = 'SPYL1'").fetchone()[0]
    assert blob == struct.pack(f"<{len(values)}d", *values)
    spyl1.values[0] = 1.0  # a read gives writable arrays of its own


def test_a_snapshot_replaces_the_set_in_one_transaction(store):
    assert store.external_snapshot() is None
    store.replace_external_series(ExternalSnapshot(T0, (ExternalSeries("SPYL1", 1, np.array([1.0]), False),)))
    second = (ExternalSeries("LEADL1_A", 2, np.array([2.0, 3.0]), False), ExternalSeries("COMPOSITE", 3, np.array([4.0]), True))
    store.replace_external_series(ExternalSnapshot(at(60), second))
    snapshot = store.external_snapshot()
    assert snapshot.taken_at == at(60)
    assert [(s.name, s.first_day, s.values.tolist()) for s in snapshot.series] == [("COMPOSITE", 3, [4.0]), ("LEADL1_A", 2, [2.0, 3.0])]
    repeated = (ExternalSeries("F", 1, np.array([1.0]), True), ExternalSeries("F", 2, np.array([2.0]), True))
    with pytest.raises(StoreError) as caught:
        store.replace_external_series(ExternalSnapshot(at(120), repeated))
    assert caught.value.conflict
    assert [s.name for s in store.external_snapshot().series] == ["COMPOSITE", "LEADL1_A"]
    assert store.external_snapshot().taken_at == at(60)


# ----- settings, events and job runs -----


def test_data_start_is_set_once(folder):
    store = Store(folder)
    assert store.data_start("US") is None
    assert store.set_data_start("US", 9000) == 9000
    assert store.set_data_start("US", 9500) == 9000
    assert store.set_data_start("TW", 9100) == 9100
    with pytest.raises(ValueError):
        store.set_data_start("JP", 9000)
    store.close()
    reopened = Store(folder)
    assert (reopened.data_start("US"), reopened.data_start("TW")) == (9000, 9100)
    reopened.close()


def test_request_logs_survive_a_reopen(folder):
    store = Store(folder)
    assert store.request_log("tiingo") == []
    store.save_request_log("tiingo", [1761717791.123456, 1761717792.5])
    store.save_request_log("finmind", [1761717000.0])
    store.save_request_log("tiingo", [1761717792.5, 1761717793.000001])
    for bad in (math.nan, math.inf):
        with pytest.raises(ValueError):
            store.save_request_log("finmind", [1761717000.0, bad])
    store.close()
    reopened = Store(folder)
    assert reopened.request_log("tiingo") == [1761717792.5, 1761717793.000001]
    assert reopened.request_log("finmind") == [1761717000.0]
    reopened.close()


def test_events_come_back_newest_first(store):
    rows = [
        (at(5), "INFO", "UPDATE_STARTED", "update started", "US", None),
        (at(1), "WARNING", "SYMBOL_FAILED", "SYMBOL_UNKNOWN Tiingo does not know XYZ", "US", "XYZ"),
        (at(9), "INFO", "UPDATE_FINISHED", "updated 3 symbols", "US", None),
        (at(2), "ERROR", "INTERNAL_ERROR", "a job failed", None, None),
        (at(7), "INFO", "BACKTEST_SAVED", "台積電 MA cross", "TW", "2330"),
    ]
    ids = [store.append_event(ts, level, kind, message, market, symbol) for ts, level, kind, message, market, symbol in rows]
    assert ids == sorted(ids)
    assert store.latest_events(3) == [
        Event(ids[4], at(7), "INFO", "BACKTEST_SAVED", "TW", "2330", "台積電 MA cross"),
        Event(ids[3], at(2), "ERROR", "INTERNAL_ERROR", None, None, "a job failed"),
        Event(ids[2], at(9), "INFO", "UPDATE_FINISHED", "US", None, "updated 3 symbols"),
    ]
    assert len(store.latest_events(50)) == 5
    with pytest.raises(ValueError):
        store.append_event(T0, "DEBUG", "UPDATE_STARTED", "x")
    with pytest.raises(ValueError):
        store.append_event(T0, "INFO", "SOMETHING_ELSE", "x")
    assert len(store.latest_events(50)) == 5


def test_event_kinds_are_those_of_section_18():
    text = (REPO / "SPEC.md").read_text(encoding="utf-8")
    block = text.split("- Event kinds:\n", 1)[1].split("\n\n", 1)[0]
    kinds = [kind for line in block.splitlines() for kind in re.findall(r"[A-Z][A-Z0-9_]+", line.split(":", 1)[1])]
    assert len(kinds) == 30
    assert EVENT_KINDS == tuple(kinds)


def test_job_runs_round_trip(store):
    first = store.start_job_run("DATA", "UPDATE_US", at(0), scheduled_for=at(-1))
    second = store.start_job_run("COMPUTE", "BACKTEST", at(1))
    third = store.start_job_run("DATA", "UPDATE_US", at(10))
    assert store.finish_job_run(first, at(5), "UPDATED", "12 symbols")
    assert store.finish_job_run(second, at(2), "DONE")
    assert not store.finish_job_run(99, at(3), "DONE")
    assert store.latest_job_runs("UPDATE_US", 10) == [
        JobRun(third, "DATA", "UPDATE_US", None, at(10), None, None, None),
        JobRun(first, "DATA", "UPDATE_US", at(-1), at(0), at(5), "UPDATED", "12 symbols"),
    ]
    assert store.latest_job_runs("UPDATE_US", 1) == [JobRun(third, "DATA", "UPDATE_US", None, at(10), None, None, None)]
    assert store.latest_job_runs("BACKTEST", 5) == [JobRun(second, "COMPUTE", "BACKTEST", None, at(1), at(2), "DONE", None)]
    assert store.latest_job_runs("UPDATE_TW", 5) == []


# ----- minute bars (§5.4, §6.6) -----


def test_minute_bars_and_coverage_round_trip(minute_store):
    ok = minute_bars([1000, 1060, 1120], [10.0, 10.5, 10.25])
    mismatch = minute_bars([90000, 90060], [20.0, 21.0])
    minute_store.save_minute_day("US", "AAPL", MinuteCoverage(10, "OK", 1.0004, at(0)), ok)
    minute_store.save_minute_day("US", "AAPL", MinuteCoverage(11, "MISMATCH", 1.9, at(1)), mismatch)
    for day, status in ((12, "EMPTY"), (13, "FAILED"), (14, "NO_PERMISSION")):
        minute_store.save_minute_day("US", "AAPL", MinuteCoverage(day, status, None, at(day)))
    read = minute_store.minute_bars("US", "AAPL")
    for field in ("t", "open", "high", "low", "close", "volume"):
        expected = np.concatenate([getattr(ok, field), getattr(mismatch, field)])
        assert_array_equal(getattr(read, field), expected, err_msg=field)
    assert read.t.dtype == np.int64 and read.close.dtype == np.float64
    assert_array_equal(minute_store.minute_bars("US", "AAPL", first_t=1060, last_t=90000).t, [1060, 1120, 90000])
    assert len(minute_store.minute_bars("TW", "AAPL").t) == 0
    assert minute_store.minute_coverage("US", "AAPL") == [
        MinuteCoverage(10, "OK", 1.0004, at(0)),
        MinuteCoverage(11, "MISMATCH", 1.9, at(1)),
        MinuteCoverage(12, "EMPTY", None, at(12)),
        MinuteCoverage(13, "FAILED", None, at(13)),
        MinuteCoverage(14, "NO_PERMISSION", None, at(14)),
    ]
    assert [c.day for c in minute_store.minute_coverage("US", "AAPL", first_day=11, last_day=13)] == [11, 12, 13]


def test_a_failed_day_can_be_saved_again_as_ok(minute_store):
    minute_store.save_minute_day("TW", "2330", MinuteCoverage(10, "FAILED", None, at(0)))
    minute_store.save_minute_day("TW", "2330", MinuteCoverage(10, "OK", 0.999, at(30)), minute_bars([5000], [600.0]))
    assert minute_store.minute_coverage("TW", "2330") == [MinuteCoverage(10, "OK", 0.999, at(30))]
    assert_array_equal(minute_store.minute_bars("TW", "2330").close, [600.0])


@pytest.mark.parametrize(
    "status, day_bars",
    [("EMPTY", minute_bars([1000], [1.0])), ("OK", None), ("MISMATCH", minute_bars([], [])), ("PARTIAL", None)],
)
def test_coverage_and_bars_must_agree(minute_store, status, day_bars):
    with pytest.raises(ValueError):
        minute_store.save_minute_day("US", "AAPL", MinuteCoverage(10, status, None, T0), day_bars)
    assert minute_store.minute_coverage("US", "AAPL") == []
    assert len(minute_store.minute_bars("US", "AAPL").t) == 0


# ----- purge (§7) -----


def test_purge_deletes_only_that_symbols_data(store, minute_store):
    for symbol in ("2330", "2317"):
        store.add_symbol("TW", symbol, "STOCK", T0)
        store.upsert_daily_bars("TW", symbol, bars([1, 2], [10.0, 11.0]))
        store.replace_actions("TW", symbol, 0, 10, [Action(2, "CASH_DIV", "DIV_RESULT", cash=1.0)])
        store.replace_short_suspensions(symbol, 0, 10, [ShortSuspension(1, 2, None)])
        store.save_contract("TW", symbol, Contract(1, "TWSE", "TWD", 1.0, 1.0, "1", T0))
        minute_store.save_minute_day("TW", symbol, MinuteCoverage(1, "OK", 1.0, T0), minute_bars([100], [10.0]))
    tw50 = store.create_list("tw50", "TW", T0)
    store.set_list_symbols(tw50, ["2330", "2317"])
    minute_store.purge_symbol("TW", "2330")
    assert store.purge_symbol("TW", "2330")
    assert [info.symbol for info in store.symbols("TW")] == ["2317"]
    assert store.daily_bar_range("TW", "2330") is None and store.actions("TW", "2330") == []
    assert store.short_suspensions("2330") == [] and store.contract("TW", "2330") is None
    assert minute_store.minute_coverage("TW", "2330") == [] and len(minute_store.minute_bars("TW", "2330").t) == 0
    assert store.daily_bar_range("TW", "2317") == (1, 2) and len(store.actions("TW", "2317")) == 1
    assert len(store.short_suspensions("2317")) == 1 and store.contract("TW", "2317") is not None
    assert len(minute_store.minute_coverage("TW", "2317")) == 1 and len(minute_store.minute_bars("TW", "2317").t) == 1
    assert store.lists()[0].symbols == ("2330", "2317")


# ----- backtest results (§17.5); the full round trip is in test_backtest.py -----


def test_a_backtest_round_trips_with_its_series_and_trades(store):
    trades = [Trade("SPY", "LONG", 10.0, 20000, 500.25, 20010, 510.5, 100.25, 2.05, 2.0, "SIGNAL"),
              Trade("SPY", "SHORT", 5.0, 20011, 511.0, 20012, 512.0, -6.0, -0.23, 1.0, "OPEN")]
    series = {"TIME": np.array([20000.0, 20001.0]), "EQUITY": np.array([1e5, math.nan])}
    saved = store.save_backtest(T0, "MA cross", "US", "1d", {"market": "US"}, {"SPY": {"bar": "2024-10-04"}},
                                {"strategy": {"sharpe": None}}, {"strategy": {"annual": []}}, {"SURVIVORSHIP": None}, series, trades)
    row = store.backtest(saved)
    assert (row.id, row.created_at, row.name, row.market, row.timeframe, row.keep_offline, row.opened_at) == (
        saved, T0, "MA cross", "US", "1d", False, None)
    assert (row.config, row.data_stamp, row.metrics, row.tables, row.flags) == (
        {"market": "US"}, {"SPY": {"bar": "2024-10-04"}}, {"strategy": {"sharpe": None}}, {"strategy": {"annual": []}},
        {"SURVIVORSHIP": None})
    read = store.backtest_series(saved)
    assert read["TIME"].tolist() == [20000.0, 20001.0] and read["EQUITY"][0] == 1e5 and math.isnan(read["EQUITY"][1])
    assert store.backtest_trades(saved) == trades
    assert store.backtest(saved + 1) is None and store.backtest_series(saved + 1) == {} and store.backtest_trades(saved + 1) == []


@pytest.mark.parametrize(
    "series, metrics",
    [({"DRAWDOWN": np.zeros(2)}, {}), ({"TIME": np.zeros(2)}, {"sharpe": math.nan})],
    ids=["unknown series", "a NaN metric"],
)
def test_an_invalid_result_is_refused_and_writes_nothing(store, series, metrics):
    with pytest.raises(ValueError):
        store.save_backtest(T0, "x", "US", "1d", {}, {}, metrics, {}, {}, series, [])
    assert store._db.execute("SELECT COUNT(*) FROM backtests").fetchone()[0] == 0
