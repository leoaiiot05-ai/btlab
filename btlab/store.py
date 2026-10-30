"""SQLite persistence: data/btlab.db and data/minute.db (SPEC.md §18).

One synchronous store class per file. Every write is one transaction, and every database failure is a StoreError.
The store reads no clock: callers pass every time from the app's injected clock (§23).
"""

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from btlab.markets import MARKETS
from btlab.records import (
    ACTION_KINDS,
    COVERAGE_STATUSES,
    EVENT_KINDS,
    EVENT_LEVELS,
    Action,
    BacktestRow,
    Contract,
    DailyBars,
    Event,
    ExternalSeries,
    ExternalSnapshot,
    JobRun,
    MinuteBars,
    MinuteCoverage,
    RiskFreeRates,
    RuleRow,
    ShortSuspension,
    SymbolInfo,
    SymbolList,
    Trade,
)

SCHEMA_VERSION = 1
SERIES_KINDS = ("TIME", "EQUITY", "BENCHMARK", "CASH", "GROSS", "NET", "POSITIONS")
BUSY_TIMEOUT_S = 1.0
# The coverage states of a day that has bars (§5.4).
WITH_BARS = ("OK", "MISMATCH")
_CONFLICTS = ("SQLITE_CONSTRAINT_PRIMARYKEY", "SQLITE_CONSTRAINT_UNIQUE")
_LOWEST, _HIGHEST = -(2**62), 2**62

log = logging.getLogger(__name__)

# §18 verbatim, plus IF NOT EXISTS, which SQLite drops from the stored schema text.
_BTLAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
  market           TEXT NOT NULL,          -- US | TW
  symbol           TEXT NOT NULL,
  name             TEXT,
  category         TEXT NOT NULL,          -- STOCK | ETF | BOND_ETF | TDR
  category_source  TEXT NOT NULL,          -- RULE | OWNER
  exchange         TEXT,                   -- TW: TWSE | TPEX
  minute_enabled   INTEGER NOT NULL DEFAULT 0,
  borrow_fee_pct   REAL,                   -- US override; NULL = the run's default
  last_fetch_at    TEXT,                   -- UTC ISO 8601
  last_fetch_error TEXT,                   -- code and English text; never a URL, key or token
  added_at         TEXT NOT NULL,
  PRIMARY KEY (market, symbol)
);
CREATE TABLE IF NOT EXISTS lists (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  market     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_members (
  list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
  symbol   TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (list_id, symbol)
);
CREATE TABLE IF NOT EXISTS daily_bars (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL NOT NULL,
  no_trade INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (market, symbol, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS actions (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  kind   TEXT NOT NULL,                    -- CASH_DIV | SHARE_RATIO
  detail TEXT NOT NULL,                    -- SPLIT | REVERSE_SPLIT | STOCK_DIV | PAR_CHANGE | CAPITAL_REDUCTION | DIV_RESULT
  cash   REAL NOT NULL DEFAULT 0,
  ratio  REAL NOT NULL DEFAULT 1,
  ref_before REAL, ref_after REAL,         -- TW dividend results
  PRIMARY KEY (market, symbol, day, kind)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS trading_days (market TEXT NOT NULL, day INTEGER NOT NULL, PRIMARY KEY (market, day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS short_suspensions (               -- TW
  symbol TEXT NOT NULL, start_day INTEGER NOT NULL, end_day INTEGER NOT NULL, reason TEXT,
  PRIMARY KEY (symbol, start_day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rf_rates (day INTEGER PRIMARY KEY, rate_pct REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS external_series (                 -- latest spylead snapshot
  name TEXT PRIMARY KEY, first_day INTEGER NOT NULL, values_blob BLOB NOT NULL,
  in_sample INTEGER NOT NULL, snapshot_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
  market TEXT NOT NULL, symbol TEXT NOT NULL, con_id INTEGER NOT NULL,
  exchange TEXT, currency TEXT NOT NULL, min_size REAL, size_increment REAL,
  market_rule_ids TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (market, symbol)
);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backtests (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
  market TEXT NOT NULL, timeframe TEXT NOT NULL,
  config TEXT NOT NULL,                    -- JSON, §10.1
  data_stamp TEXT NOT NULL,                -- JSON
  metrics TEXT NOT NULL, tables TEXT NOT NULL, flags TEXT NOT NULL,   -- JSON
  keep_offline INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT                           -- last GET of the detail; drives the offline set (§20.5)
);
CREATE TABLE IF NOT EXISTS backtest_series (
  backtest_id INTEGER NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                      -- TIME | EQUITY | BENCHMARK | CASH | GROSS | NET | POSITIONS
  values_blob BLOB NOT NULL,
  PRIMARY KEY (backtest_id, kind)
);
CREATE TABLE IF NOT EXISTS trades (
  backtest_id INTEGER NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
  entry_time INTEGER NOT NULL, entry_price REAL NOT NULL,
  exit_time INTEGER, exit_price REAL, pnl REAL, return_pct REAL, costs REAL NOT NULL,
  exit_reason TEXT NOT NULL,
  PRIMARY KEY (backtest_id, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS optimizations (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL, market TEXT NOT NULL,
  mode TEXT NOT NULL,                      -- GRID | OOS | WFO
  status TEXT NOT NULL,                    -- DONE | CANCELLED | INTERRUPTED | FAILED
  config TEXT NOT NULL, results TEXT NOT NULL, best TEXT, flags TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_portfolios (
  id INTEGER PRIMARY KEY, market TEXT NOT NULL, mode TEXT NOT NULL,
  backtest_id INTEGER, config TEXT NOT NULL, capital REAL NOT NULL,
  created_at TEXT NOT NULL, first_day INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_portfolio ON live_portfolios (market, mode) WHERE active = 1;
CREATE TABLE IF NOT EXISTS live_batches (
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES live_portfolios(id),
  session_day INTEGER NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL,                    -- PROPOSED | CONFIRMED | SENT | DONE | EXPIRED | REPLACED
  confirmed_at TEXT, positions_seen TEXT NOT NULL   -- JSON: the holdings the batch was built on
);
CREATE TABLE IF NOT EXISTS live_orders (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES live_batches(id),
  line INTEGER NOT NULL, symbol TEXT NOT NULL, con_id INTEGER,
  action TEXT NOT NULL, quantity REAL NOT NULL, kind TEXT NOT NULL,   -- MOO | LIMIT
  limit_price REAL, ref_price REAL NOT NULL,
  status TEXT NOT NULL, reason TEXT,
  order_ref TEXT UNIQUE, ib_order_id INTEGER,
  filled_qty REAL NOT NULL DEFAULT 0, avg_price REAL, commission REAL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_holdings (                   -- test mode: the virtual book
  portfolio_id INTEGER NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
  PRIMARY KEY (portfolio_id, symbol)
);
CREATE TABLE IF NOT EXISTS live_ledger (
  portfolio_id INTEGER NOT NULL, day INTEGER NOT NULL,
  cash REAL NOT NULL, equity REAL NOT NULL, holdings TEXT NOT NULL,  -- JSON
  PRIMARY KEY (portfolio_id, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ibguard_exclusions (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES live_orders(id),
  con_id INTEGER NOT NULL, symbol TEXT NOT NULL,
  state TEXT NOT NULL,                     -- ACTIVE | RESTORED | CLOSED
  excluded_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY, lane TEXT NOT NULL, kind TEXT NOT NULL,
  scheduled_for TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- data_start/<market>, request logs, ...
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  level TEXT NOT NULL,                     -- INFO | WARNING | ERROR
  kind TEXT NOT NULL, market TEXT, symbol TEXT, message TEXT NOT NULL
);
"""

_MINUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bars (
  market TEXT NOT NULL, symbol TEXT NOT NULL, t INTEGER NOT NULL,   -- UTC epoch second of the bar's start
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
  PRIMARY KEY (market, symbol, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS minute_coverage (
  market TEXT NOT NULL, symbol TEXT NOT NULL, day INTEGER NOT NULL,
  status TEXT NOT NULL,                    -- OK | EMPTY | FAILED | NO_PERMISSION | MISMATCH
  factor REAL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (market, symbol, day)
) WITHOUT ROWID;
"""


class StoreError(Exception):
    """A database cannot be opened, read or written. The message names the file and holds SQLite's text, never a bound value.

    conflict is true when a UNIQUE or PRIMARY KEY constraint failed, for example a list name already in use.
    """

    def __init__(self, message: str, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


class _Database:
    """One SQLite file: its connection, pragmas, schema and transactions (§18)."""

    FILE_NAME = ""
    SCHEMA = ""

    def __init__(self, folder: Path) -> None:
        self.path = Path(folder) / self.FILE_NAME
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StoreError(f"{self.path}: cannot create its folder ({error.strerror or type(error).__name__})") from error
        try:
            # timeout sets the busy timeout; with autocommit=True every transaction is explicit.
            self._db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, autocommit=True)
        except sqlite3.Error as error:
            raise self._error(error) from error
        try:
            self._prepare()
        except BaseException as error:
            # Windows cannot delete a file while a connection holds it.
            self._db.close()
            if isinstance(error, sqlite3.Error):
                raise self._error(error) from error
            raise

    def close(self) -> None:
        self._db.close()

    def _prepare(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(f"{self.path}: schema version {version} is newer than this btlab's {SCHEMA_VERSION}")
        self._choose_journal()
        # SQLite enforces foreign keys, and so ON DELETE CASCADE, only with this pragma.
        self._db.execute("PRAGMA foreign_keys = ON")
        if version < SCHEMA_VERSION:
            with self._write() as db:
                # executescript runs inside the explicit transaction; splitting on ";" would break on a comment.
                db.executescript(self.SCHEMA)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _choose_journal(self) -> None:
        """WAL with synchronous NORMAL, or DELETE with FULL where WAL cannot be enabled (some external drives)."""
        try:
            mode = self._db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            reason = f"the journal mode stayed {mode}"
        except sqlite3.OperationalError as error:
            mode, reason = None, str(error)
        if mode == "wal":
            self._db.execute("PRAGMA synchronous = NORMAL")
            return
        log.warning("%s: WAL cannot be enabled (%s); using journal mode DELETE", self.path, reason)
        if self._db.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
            raise StoreError(f"{self.path}: neither journal mode WAL nor DELETE can be enabled")
        # With NORMAL, a power loss can corrupt a rollback-journal database on older file systems.
        self._db.execute("PRAGMA synchronous = FULL")

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise self._error(error) from error

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction: committed when the block ends, rolled back on any exception, so a failed write changes nothing."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield self._db
            self._db.execute("COMMIT")
        except BaseException as error:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise self._error(error) from error
            raise

    def _changes(self, sql: str, rows: list[tuple]) -> int:
        """Runs sql once per row in one transaction, and counts the rows it added, changed or removed."""
        with self._write() as db:
            before = db.total_changes
            db.executemany(sql, rows)
            return db.total_changes - before

    def _error(self, error: sqlite3.Error) -> StoreError:
        return StoreError(f"{self.path}: {error}", conflict=getattr(error, "sqlite_errorname", None) in _CONFLICTS)


class Store(_Database):
    """data/btlab.db (§18). Results, rules, optimizations and live tables get access functions in the phases that use them.

    Writes that decide a data update's outcome return the number of rows added, changed or removed, because an
    update that stores nothing new is UP_TO_DATE (§17.2).
    """

    FILE_NAME = "btlab.db"
    SCHEMA = _BTLAB_SCHEMA

    # ----- symbols (§7) -----

    def add_symbol(self, market: str, symbol: str, category: str, added_at: datetime) -> bool:
        """Adds a symbol with its category by rule. An existing symbol stays as it was, and the answer is False."""
        _check_category(market, category)
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO symbols (market, symbol, category, category_source, added_at) VALUES (?, ?, ?, 'RULE', ?)"
                " ON CONFLICT (market, symbol) DO NOTHING",
                (market, symbol, category, _iso(added_at)),
            )
        return cursor.rowcount == 1

    def symbols(self, market: str) -> list[SymbolInfo]:
        rows = self._read(
            "SELECT market, symbol, name, category, category_source, exchange, minute_enabled, borrow_fee_pct,"
            " last_fetch_at, last_fetch_error, added_at FROM symbols WHERE market = ? ORDER BY symbol",
            (market,),
        )
        return [_symbol_info(row) for row in rows]

    def set_symbol_details(self, market: str, symbol: str, name: str | None, exchange: str | None) -> bool:
        """The name and the TW exchange from TaiwanStockInfo (§5.2). False for an unknown symbol."""
        return self._update_symbol(market, symbol, "name = ?, exchange = ?", (name, exchange))

    def set_category(self, market: str, symbol: str, category: str) -> bool:
        """The owner's override of the category (§7). False for an unknown symbol."""
        _check_category(market, category)
        return self._update_symbol(market, symbol, "category = ?, category_source = 'OWNER'", (category,))

    def set_minute_enabled(self, market: str, symbol: str, enabled: bool) -> bool:
        return self._update_symbol(market, symbol, "minute_enabled = ?", (int(enabled),))

    def set_borrow_fee(self, market: str, symbol: str, pct: float | None) -> bool:
        """A US symbol's borrow fee in percent a year; None means the run's default (§10.7)."""
        if pct is not None and not (math.isfinite(pct) and pct >= 0):
            raise ValueError("a borrow fee must be a finite percentage of at least 0")
        return self._update_symbol(market, symbol, "borrow_fee_pct = ?", (pct,))

    def record_fetch(self, market: str, symbol: str, at: datetime, error: str | None) -> bool:
        """error is SourceError's code and text (§5.7); None clears it."""
        return self._update_symbol(market, symbol, "last_fetch_at = ?, last_fetch_error = ?", (_iso(at), error))

    def purge_symbol(self, market: str, symbol: str) -> bool:
        """Deletes the symbol with its bars, actions, contract and TW short suspensions (§7). Lists keep it.

        Minute bars live in minute.db: the caller purges MinuteStore first, so a failure here can be repeated.
        """
        with self._write() as db:
            cursor = db.execute("DELETE FROM symbols WHERE market = ? AND symbol = ?", (market, symbol))
            for table in ("daily_bars", "actions", "contracts"):
                db.execute(f"DELETE FROM {table} WHERE market = ? AND symbol = ?", (market, symbol))
            if market == "TW":
                db.execute("DELETE FROM short_suspensions WHERE symbol = ?", (symbol,))
        return cursor.rowcount == 1

    def _update_symbol(self, market: str, symbol: str, assignments: str, values: tuple) -> bool:
        with self._write() as db:
            cursor = db.execute(f"UPDATE symbols SET {assignments} WHERE market = ? AND symbol = ?", (*values, market, symbol))
        return cursor.rowcount == 1

    # ----- lists (§7) -----

    def create_list(self, name: str, market: str, created_at: datetime) -> int:
        """A name already in use raises StoreError with conflict."""
        _check_market(market)
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO lists (name, market, created_at) VALUES (?, ?, ?)", (name, market, _iso(created_at))
            )
        return cursor.lastrowid

    def rename_list(self, list_id: int, name: str) -> bool:
        with self._write() as db:
            cursor = db.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))
        return cursor.rowcount == 1

    def set_list_symbols(self, list_id: int, symbols: Sequence[str]) -> bool:
        """Replaces the members, in the given order. False for an unknown list."""
        if len(set(symbols)) != len(symbols):
            raise ValueError("a symbol appears twice in the list")
        with self._write() as db:
            if not db.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchall():
                return False
            db.execute("DELETE FROM list_members WHERE list_id = ?", (list_id,))
            db.executemany(
                "INSERT INTO list_members (list_id, symbol, position) VALUES (?, ?, ?)",
                [(list_id, symbol, position) for position, symbol in enumerate(symbols)],
            )
        return True

    def delete_list(self, list_id: int) -> bool:
        """The members go with the list (ON DELETE CASCADE)."""
        with self._write() as db:
            cursor = db.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        return cursor.rowcount == 1

    def lists(self, market: str | None = None) -> list[SymbolList]:
        """Every list, or a market's, in the order of creation, each with its symbols in list order."""
        rows = self._read(
            "SELECT l.id, l.name, l.market, l.created_at, m.symbol FROM lists AS l"
            " LEFT JOIN list_members AS m ON m.list_id = l.id"
            " WHERE ?1 IS NULL OR l.market = ?1 ORDER BY l.id, m.position",
            (market,),
        )
        heads: dict[int, tuple[str, str, str]] = {}
        members: dict[int, list[str]] = {}
        for list_id, name, list_market, created_at, symbol in rows:
            heads[list_id] = (name, list_market, created_at)
            members.setdefault(list_id, [])
            if symbol is not None:
                members[list_id].append(symbol)
        return [
            SymbolList(list_id, name, list_market, _moment(created_at), tuple(members[list_id]))
            for list_id, (name, list_market, created_at) in heads.items()
        ]

    # ----- daily bars (§5.1, §5.2, §6.1) -----

    def upsert_daily_bars(self, market: str, symbol: str, bars: DailyBars) -> int:
        """Inserts new days and replaces the stored days of the window. Every other stored day stays, so a short
        answer never deletes history. An unchanged day counts 0."""
        _check_market(market)
        columns = _columns(bars.day, bars.open, bars.high, bars.low, bars.close, bars.volume, bars.no_trade)
        rows = [
            (market, symbol, day, _real(open_), _real(high), _real(low), _real(close), _real(volume), int(no_trade))
            for day, open_, high, low, close, volume, no_trade in zip(*columns)
        ]
        return self._changes(
            "INSERT INTO daily_bars (market, symbol, day, open, high, low, close, volume, no_trade)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (market, symbol, day) DO UPDATE SET"
            " open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,"
            " volume = excluded.volume, no_trade = excluded.no_trade"
            " WHERE daily_bars.open IS NOT excluded.open OR daily_bars.high IS NOT excluded.high"
            " OR daily_bars.low IS NOT excluded.low OR daily_bars.close IS NOT excluded.close"
            " OR daily_bars.volume IS NOT excluded.volume OR daily_bars.no_trade IS NOT excluded.no_trade",
            rows,
        )

    def daily_bars(self, market: str, symbol: str, first_day: int | None = None, last_day: int | None = None) -> DailyBars:
        """Raw bars with inclusive bounds. A TW no-trade day has NaN prices."""
        rows = self._read(
            "SELECT day, open, high, low, close, volume, no_trade FROM daily_bars"
            " WHERE market = ? AND symbol = ? AND day BETWEEN ? AND ? ORDER BY day",
            (market, symbol, *_bounds(first_day, last_day)),
        )
        day, open_, high, low, close, volume, no_trade = _float_columns(rows, 7)
        return DailyBars(day.astype(np.int64), open_, high, low, close, volume, no_trade.astype(bool))

    def daily_bar_range(self, market: str, symbol: str) -> tuple[int, int] | None:
        """The first and last stored day, or None without bars."""
        ((first, last),) = self._read(
            "SELECT MIN(day), MAX(day) FROM daily_bars WHERE market = ? AND symbol = ?", (market, symbol)
        )
        return None if first is None else (first, last)

    # ----- corporate actions (§6.2) -----

    def replace_actions(self, market: str, symbol: str, first_day: int, last_day: int, actions: Sequence[Action]) -> int:
        """Replaces the stored actions of the window [first_day, last_day], so a corrected or withdrawn action does not
        linger. Returns the number of actions added, changed or removed."""
        _check_market(market)
        for action in actions:
            if not first_day <= action.day <= last_day:
                raise ValueError("an action lies outside the window")
            if action.kind not in ACTION_KINDS:
                raise ValueError(f"unknown action kind {action.kind}")
        new = _keyed(actions, _action_key)
        with self._write() as db:
            count = _difference(_keyed(self.actions(market, symbol, first_day, last_day), _action_key), new)
            if count:
                db.execute(
                    "DELETE FROM actions WHERE market = ? AND symbol = ? AND day BETWEEN ? AND ?",
                    (market, symbol, first_day, last_day),
                )
                db.executemany(
                    "INSERT INTO actions (market, symbol, day, kind, detail, cash, ratio, ref_before, ref_after)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(market, symbol, a.day, a.kind, a.detail, a.cash, a.ratio, a.ref_before, a.ref_after) for a in actions],
                )
        return count

    def actions(self, market: str, symbol: str, first_day: int | None = None, last_day: int | None = None) -> list[Action]:
        """Sorted by day and kind, with inclusive bounds."""
        rows = self._read(
            "SELECT day, kind, detail, cash, ratio, ref_before, ref_after FROM actions"
            " WHERE market = ? AND symbol = ? AND day BETWEEN ? AND ? ORDER BY day, kind",
            (market, symbol, *_bounds(first_day, last_day)),
        )
        return [Action(*row) for row in rows]

    # ----- calendars (§4.3) -----

    def replace_trading_days(self, market: str, days: Iterable[int]) -> int:
        """Replaces the market's whole calendar: TaiwanStockTradingDate answers with all of it. Returns the number of
        days added or removed. US trading days are the days of the stored SPY bars instead."""
        _check_market(market)
        new = {int(day) for day in days}
        with self._write() as db:
            stored = set(self.trading_days(market).tolist())
            removed, added = sorted(stored - new), sorted(new - stored)
            db.executemany("DELETE FROM trading_days WHERE market = ? AND day = ?", [(market, day) for day in removed])
            db.executemany("INSERT INTO trading_days (market, day) VALUES (?, ?)", [(market, day) for day in added])
        return len(removed) + len(added)

    def trading_days(self, market: str) -> np.ndarray:
        """int64 days, ascending."""
        rows = self._read("SELECT day FROM trading_days WHERE market = ? ORDER BY day", (market,))
        return np.array([day for (day,) in rows], dtype=np.int64)

    # ----- TW short-sale suspensions (§5.2, §10.7) -----

    def replace_short_suspensions(
        self, symbol: str, first_day: int, last_day: int, suspensions: Sequence[ShortSuspension]
    ) -> int:
        """Replaces the suspensions that start inside [first_day, last_day]. Returns the number added, changed or removed."""
        if any(not first_day <= suspension.start_day <= last_day for suspension in suspensions):
            raise ValueError("a suspension starts outside the window")
        new = _keyed(suspensions, _start_day)
        with self._write() as db:
            stored = [s for s in self.short_suspensions(symbol) if first_day <= s.start_day <= last_day]
            count = _difference(_keyed(stored, _start_day), new)
            if count:
                db.execute(
                    "DELETE FROM short_suspensions WHERE symbol = ? AND start_day BETWEEN ? AND ?",
                    (symbol, first_day, last_day),
                )
                db.executemany(
                    "INSERT INTO short_suspensions (symbol, start_day, end_day, reason) VALUES (?, ?, ?, ?)",
                    [(symbol, s.start_day, s.end_day, s.reason) for s in suspensions],
                )
        return count

    def short_suspensions(self, symbol: str) -> list[ShortSuspension]:
        rows = self._read(
            "SELECT start_day, end_day, reason FROM short_suspensions WHERE symbol = ? ORDER BY start_day", (symbol,)
        )
        return [ShortSuspension(*row) for row in rows]

    # ----- risk-free rates (§5.3) -----

    def upsert_rf_rates(self, rates: RiskFreeRates) -> int:
        """Inserts new days and replaces changed ones; returns the number of days added or changed."""
        days, values = _columns(rates.day, rates.rate_pct)
        return self._changes(
            "INSERT INTO rf_rates (day, rate_pct) VALUES (?, ?) ON CONFLICT (day) DO UPDATE SET"
            " rate_pct = excluded.rate_pct WHERE rf_rates.rate_pct IS NOT excluded.rate_pct",
            [(day, _real(value)) for day, value in zip(days, values)],
        )

    def rf_rates(self, first_day: int | None = None, last_day: int | None = None) -> RiskFreeRates:
        rows = self._read(
            "SELECT day, rate_pct FROM rf_rates WHERE day BETWEEN ? AND ? ORDER BY day", _bounds(first_day, last_day)
        )
        day, rate_pct = _float_columns(rows, 2)
        return RiskFreeRates(day.astype(np.int64), rate_pct)

    # ----- IB contracts (§5.4) -----

    def save_contract(self, market: str, symbol: str, contract: Contract) -> None:
        _check_market(market)
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO contracts (market, symbol, con_id, exchange, currency, min_size, size_increment,"
                " market_rule_ids, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    market, symbol, contract.con_id, contract.exchange, contract.currency, contract.min_size,
                    contract.size_increment, contract.market_rule_ids, _iso(contract.fetched_at),
                ),
            )

    def contract(self, market: str, symbol: str) -> Contract | None:
        rows = self._read(
            "SELECT con_id, exchange, currency, min_size, size_increment, market_rule_ids, fetched_at FROM contracts"
            " WHERE market = ? AND symbol = ?",
            (market, symbol),
        )
        if not rows:
            return None
        *fields, fetched_at = rows[0]
        return Contract(*fields, _moment(fetched_at))

    # ----- spylead series (§5.6) -----

    def replace_external_series(self, snapshot: ExternalSnapshot) -> None:
        """Replaces the stored set with the snapshot's series in one transaction."""
        taken_at = _iso(snapshot.taken_at)
        rows = [(s.name, int(s.first_day), _blob(s.values), int(s.in_sample), taken_at) for s in snapshot.series]
        with self._write() as db:
            db.execute("DELETE FROM external_series")
            db.executemany(
                "INSERT INTO external_series (name, first_day, values_blob, in_sample, snapshot_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def external_snapshot(self) -> ExternalSnapshot | None:
        """The stored series by name, or None when none is stored."""
        rows = self._read("SELECT name, first_day, values_blob, in_sample, snapshot_at FROM external_series ORDER BY name")
        if not rows:
            return None
        series = tuple(ExternalSeries(name, first, _array(blob), bool(in_sample)) for name, first, blob, in_sample, _ in rows)
        return ExternalSnapshot(_moment(rows[0][4]), series)

    # ----- settings (§4.1, §5.1, §5.2) -----

    def data_start(self, market: str) -> int | None:
        return self._setting(f"data_start/{market}")

    def set_data_start(self, market: str, day: int) -> int:
        """Stores the market's data_start unless one is stored, and returns the stored day: the first value stays."""
        _check_market(market)
        key = f"data_start/{market}"
        with self._write() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (key, _json(int(day)))
            )
            return self._setting(key)

    def request_log(self, source: str) -> list[float]:
        """The UTC epoch seconds of a source's past requests, kept so a restart does not reset its budget."""
        return self._setting(f"request_log/{source}") or []

    def save_request_log(self, source: str, times: Sequence[float]) -> None:
        value = _json([float(moment) for moment in times])
        with self._write() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (f"request_log/{source}", value),
            )

    def _setting(self, key: str) -> Any:
        rows = self._read("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else None

    # ----- rules (§9.6) -----

    def create_rule(self, name: str, rule: dict, updated_at: datetime) -> int:
        """A name already in use raises StoreError with conflict."""
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO rules (name, body, updated_at) VALUES (?, ?, ?)", (name, _json(rule), _iso(updated_at))
            )
        return cursor.lastrowid

    def update_rule(self, rule_id: int, name: str, rule: dict, updated_at: datetime) -> bool:
        """False for an unknown rule; a name another rule uses raises StoreError with conflict."""
        with self._write() as db:
            cursor = db.execute(
                "UPDATE rules SET name = ?, body = ?, updated_at = ? WHERE id = ?",
                (name, _json(rule), _iso(updated_at), rule_id),
            )
        return cursor.rowcount == 1

    def delete_rule(self, rule_id: int) -> bool:
        with self._write() as db:
            cursor = db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return cursor.rowcount == 1

    def rules(self) -> list[RuleRow]:
        """Every rule, by name."""
        rows = self._read("SELECT id, name, body, updated_at FROM rules ORDER BY name")
        return [RuleRow(id_, name, json.loads(body), _moment(updated_at)) for id_, name, body, updated_at in rows]

    # ----- backtest results (§10.11, §17.5) -----

    def save_backtest(
        self,
        created_at: datetime,
        name: str,
        market: str,
        timeframe: str,
        config: dict,
        data_stamp: dict,
        metrics: dict,
        tables: dict,
        flags: dict,
        series: dict[str, np.ndarray],
        trades: Sequence[Trade],
    ) -> int:
        """Saves a result with its series (float64 BLOBs) and its trades in one transaction; returns its id."""
        unknown = set(series) - set(SERIES_KINDS)
        if unknown:
            raise ValueError(f"unknown series {sorted(unknown)}")
        documents = [_json(document) for document in (config, data_stamp, metrics, tables, flags)]
        blobs = [(kind, _blob(values)) for kind, values in series.items()]
        with self._write() as db:
            backtest_id = db.execute(
                "INSERT INTO backtests (created_at, name, market, timeframe, config, data_stamp, metrics, tables, flags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_iso(created_at), name, market, timeframe, *documents),
            ).lastrowid
            db.executemany(
                "INSERT INTO backtest_series (backtest_id, kind, values_blob) VALUES (?, ?, ?)",
                [(backtest_id, kind, blob) for kind, blob in blobs],
            )
            db.executemany(
                "INSERT INTO trades (backtest_id, seq, symbol, side, quantity, entry_time, entry_price, exit_time,"
                " exit_price, pnl, return_pct, costs, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (backtest_id, seq, t.symbol, t.side, t.quantity, t.entry_time, t.entry_price, t.exit_time,
                     _real(t.exit_price), _real(t.pnl), _real(t.return_pct), t.costs, t.exit_reason)
                    for seq, t in enumerate(trades, 1)
                ],
            )
        return backtest_id

    def backtest(self, backtest_id: int) -> BacktestRow | None:
        rows = self._read(
            "SELECT id, created_at, name, market, timeframe, config, data_stamp, metrics, tables, flags, keep_offline,"
            " opened_at FROM backtests WHERE id = ?",
            (backtest_id,),
        )
        if not rows:
            return None
        id_, created_at, name, market, timeframe, *documents, keep_offline, opened_at = rows[0]
        config, data_stamp, metrics, tables, flags = (json.loads(document) for document in documents)
        return BacktestRow(
            id_, _moment(created_at), name, market, timeframe, config, data_stamp, metrics, tables, flags,
            bool(keep_offline), _moment(opened_at),
        )

    def backtest_series(self, backtest_id: int) -> dict[str, np.ndarray]:
        rows = self._read("SELECT kind, values_blob FROM backtest_series WHERE backtest_id = ?", (backtest_id,))
        return {kind: _array(blob) for kind, blob in rows}

    def backtest_trades(self, backtest_id: int, offset: int = 0, limit: int | None = None) -> list[Trade]:
        """A result's trades in order; limit None reads them all."""
        rows = self._read(
            "SELECT symbol, side, quantity, entry_time, entry_price, exit_time, exit_price, pnl, return_pct, costs,"
            " exit_reason FROM trades WHERE backtest_id = ? ORDER BY seq LIMIT ? OFFSET ?",
            (backtest_id, -1 if limit is None else limit, offset),
        )
        return [Trade(*row) for row in rows]

    # ----- events and job runs (§17.1, §18) -----

    def append_event(
        self, ts: datetime, level: str, kind: str, message: str, market: str | None = None, symbol: str | None = None
    ) -> int:
        """Only the levels and kinds of §18, so the UI can localize every stored code (§20.4)."""
        if level not in EVENT_LEVELS:
            raise ValueError(f"unknown event level {level}")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind}")
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO events (ts, level, kind, market, symbol, message) VALUES (?, ?, ?, ?, ?, ?)",
                (_iso(ts), level, kind, market, symbol, message),
            )
        return cursor.lastrowid

    def latest_events(self, limit: int) -> list[Event]:
        """Newest first, by id rather than by ts."""
        rows = self._read(
            "SELECT id, ts, level, kind, market, symbol, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [Event(id_, _moment(ts), *rest) for id_, ts, *rest in rows]

    def start_job_run(self, lane: str, kind: str, started_at: datetime, scheduled_for: datetime | None = None) -> int:
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO job_runs (lane, kind, scheduled_for, started_at) VALUES (?, ?, ?, ?)",
                (lane, kind, _iso(scheduled_for), _iso(started_at)),
            )
        return cursor.lastrowid

    def finish_job_run(self, run_id: int, finished_at: datetime, outcome: str, message: str | None = None) -> bool:
        with self._write() as db:
            cursor = db.execute(
                "UPDATE job_runs SET finished_at = ?, outcome = ?, message = ? WHERE id = ?",
                (_iso(finished_at), outcome, message, run_id),
            )
        return cursor.rowcount == 1

    def latest_job_runs(self, kind: str, limit: int) -> list[JobRun]:
        """A kind's runs, newest first."""
        rows = self._read(
            "SELECT id, lane, kind, scheduled_for, started_at, finished_at, outcome, message FROM job_runs"
            " WHERE kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit),
        )
        return [
            JobRun(id_, lane, kind_, _moment(scheduled_for), _moment(started_at), _moment(finished_at), outcome, message)
            for id_, lane, kind_, scheduled_for, started_at, finished_at, outcome, message in rows
        ]


class MinuteStore(_Database):
    """data/minute.db: 1-minute bars in raw prices and their coverage (§5.4, §6.6)."""

    FILE_NAME = "minute.db"
    SCHEMA = _MINUTE_SCHEMA

    def save_minute_day(self, market: str, symbol: str, coverage: MinuteCoverage, bars: MinuteBars | None = None) -> None:
        """Writes a day's bars and its coverage in one transaction, so coverage OK never exists without its bars.

        Only OK and MISMATCH days have bars, and they must.
        """
        _check_market(market)
        if coverage.status not in COVERAGE_STATUSES:
            raise ValueError(f"unknown coverage status {coverage.status}")
        columns = _columns(bars.t, bars.open, bars.high, bars.low, bars.close, bars.volume) if bars is not None else []
        if bool(columns and columns[0]) != (coverage.status in WITH_BARS):
            needs = "needs" if coverage.status in WITH_BARS else "has no"
            raise ValueError(f"a day with coverage {coverage.status} {needs} bars")
        rows = [(market, symbol, t, *map(_real, values)) for t, *values in zip(*columns)]
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO minute_bars (market, symbol, t, open, high, low, close, volume)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            db.execute(
                "INSERT OR REPLACE INTO minute_coverage (market, symbol, day, status, factor, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (market, symbol, int(coverage.day), coverage.status, coverage.factor, _iso(coverage.fetched_at)),
            )

    def minute_bars(self, market: str, symbol: str, first_t: int | None = None, last_t: int | None = None) -> MinuteBars:
        """Bars ordered by t, with inclusive bounds in UTC epoch seconds."""
        rows = self._read(
            "SELECT t, open, high, low, close, volume FROM minute_bars"
            " WHERE market = ? AND symbol = ? AND t BETWEEN ? AND ? ORDER BY t",
            (market, symbol, *_bounds(first_t, last_t)),
        )
        t, *prices = _float_columns(rows, 6)
        return MinuteBars(t.astype(np.int64), *prices)

    def minute_coverage(
        self, market: str, symbol: str, first_day: int | None = None, last_day: int | None = None
    ) -> list[MinuteCoverage]:
        """Sorted by day, with inclusive bounds."""
        rows = self._read(
            "SELECT day, status, factor, fetched_at FROM minute_coverage"
            " WHERE market = ? AND symbol = ? AND day BETWEEN ? AND ? ORDER BY day",
            (market, symbol, *_bounds(first_day, last_day)),
        )
        return [MinuteCoverage(day, status, factor, _moment(fetched_at)) for day, status, factor, fetched_at in rows]

    def purge_symbol(self, market: str, symbol: str) -> None:
        with self._write() as db:
            for table in ("minute_bars", "minute_coverage"):
                db.execute(f"DELETE FROM {table} WHERE market = ? AND symbol = ?", (market, symbol))


def _check_market(market: str) -> None:
    if market not in MARKETS:
        raise ValueError(f"unknown market {market}")


def _check_category(market: str, category: str) -> None:
    _check_market(market)
    if category not in MARKETS[market].categories:
        raise ValueError(f"{market} has no category {category}")


def _symbol_info(row: tuple) -> SymbolInfo:
    market, symbol, name, category, source, exchange, minute, fee, fetched_at, error, added_at = row
    return SymbolInfo(
        market, symbol, name, category, source, exchange, bool(minute), fee, _moment(fetched_at), error, _moment(added_at)
    )


def _action_key(action: Action) -> tuple[int, str]:
    return action.day, action.kind


def _start_day(suspension: ShortSuspension) -> int:
    return suspension.start_day


def _keyed(records: Sequence[Any], key: Callable[[Any], Hashable]) -> dict[Hashable, Any]:
    keyed = {key(record): record for record in records}
    if len(keyed) != len(records):
        raise ValueError("two records share a key")
    return keyed


def _difference(stored: dict[Hashable, Any], new: dict[Hashable, Any]) -> int:
    """How many records a replacement adds, changes or removes."""
    return len(stored.keys() ^ new.keys()) + sum(stored[key] != new[key] for key in stored.keys() & new.keys())


def _iso(moment: datetime | None) -> str | None:
    """UTC ISO 8601 text with microseconds and Z (§23)."""
    if moment is None:
        return None
    if moment.utcoffset() is None:
        raise ValueError("times must be aware datetimes")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _moment(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _real(value: float) -> float | None:
    """NaN binds as NULL; NULL reads back as NaN in float arrays."""
    return None if math.isnan(value) else value


def _bounds(first: int | None, last: int | None) -> tuple[int, int]:
    return (_LOWEST if first is None else int(first), _HIGHEST if last is None else int(last))


def _columns(*arrays: Any) -> list[list]:
    """1-D arrays of one length, as lists of Python values that sqlite3 can bind."""
    columns = [np.asarray(array) for array in arrays]
    if any(column.ndim != 1 for column in columns) or len({len(column) for column in columns}) > 1:
        raise ValueError("the arrays must be 1-D and of one length")
    return [column.tolist() for column in columns]


def _float_columns(rows: list[tuple], count: int) -> list[np.ndarray]:
    """The columns of rows as float64 arrays; NULL becomes NaN."""
    if not rows:
        return [np.empty(0) for _ in range(count)]
    return [np.array(column, dtype=np.float64) for column in zip(*rows)]


def _blob(values: Any) -> bytes:
    """Arrays are stored as float64 little-endian BLOBs (§18)."""
    array = np.asarray(values, dtype="<f8")
    if array.ndim != 1:
        raise ValueError("a stored array must be 1-D")
    return array.tobytes()


def _array(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f8").astype(np.float64)


def _json(value: Any) -> str:
    """Settings values; NaN and infinities are refused, because JSON has no such numbers."""
    return json.dumps(value, allow_nan=False)
