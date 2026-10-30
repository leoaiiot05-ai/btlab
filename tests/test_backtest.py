"""One backtest from configuration to saved result (SPEC.md §10.1, §10.9, §10.11, §12, §17.5, AC-11, AC-29)."""

from datetime import date, datetime, timezone

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from btlab.backtest import (
    EQUITY_COLUMNS,
    TRADES_COLUMNS,
    RunConfigError,
    RunError,
    equity_csv,
    load_run,
    run_backtest,
    save_result,
    strategy_factory,
    trades_csv,
    validate_config,
)
from btlab.engine import Targets
from btlab.indicators import sma
from btlab.records import Trade, to_day
from btlab.store import Store
from builders import NOW, TAXES, WEEKDAYS, put_bars, synthetic_store, walk

@pytest.fixture
def store(tmp_path):
    opened = synthetic_store(tmp_path)
    yield opened
    opened.close()


def config(**changes) -> dict:
    return {"name": "MA cross", "market": "US", "symbols": ["AAA", "BBB"], "start": "2019-09-03",
            "strategy": {"source": "builtin", "id": "MA_CROSS"}, **changes}


def ma_cross(strategy_view):
    close = strategy_view.bars.close
    return Targets("DIRECTION", np.where(sma(close, 10) > sma(close, 30), 1.0, 0.0))


def run(store: Store, **changes):
    validated = validate_config(config(**changes))
    return run_backtest(validated, load_run(store, None, validated), TAXES, ma_cross)


# ----- the configuration -----


@pytest.mark.parametrize(
    "changes, path",
    [
        ({"market": "JP"}, "market"),
        ({"symbols": []}, "symbols"),
        ({"symbols": ["AAA", "bad symbol"]}, "symbols[1]"),
        ({"symbols": ["AAA", "aaa"]}, "symbols[1]"),
        ({"start": "2019-13-01"}, "start"),
        ({"end": "2019-01-01"}, "end"),
        ({"color": "red"}, "color"),
        ({"timeframe": "5m"}, "timeframe"),
        ({"intraday": {"close_out": "NONE"}}, "intraday"),
        ({"strategy": {"source": "magic", "id": "X"}}, "strategy.source"),
        ({"strategy": {"source": "builtin"}}, "strategy.id"),
        ({"strategy": {"source": "builtin", "id": "MA_CROSS", "params": [1]}}, "strategy.params"),
        ({"strategy": {"source": "builtin", "id": "NOPE"}}, "strategy.id"),
        ({"strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 2.5}}}, "strategy.params.fast"),
        ({"strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 60}}}, "strategy.params.fast"),
        ({"strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"speed": 1}}}, "strategy.params.speed"),
        ({"strategy": {"source": "builtin", "id": "RSI", "params": {"upper": True}}}, "strategy.params.upper"),
        ({"strategy": {"source": "builtin", "id": "DUAL_MOMENTUM", "params": {"safe": "TLT"}}}, "strategy.params.safe"),
        ({"sizing": {"mode": "KELLY"}}, "sizing.mode"),
        ({"sizing": {"mode": "FIXED_AMOUNT"}}, "sizing.amount"),
        ({"sizing": {"mode": "FIXED_FRACTION", "fraction": 1.5}}, "sizing.fraction"),
        ({"sizing": {"mode": "FIXED_FRACTION", "amount": 5}}, "sizing.amount"),
        ({"sizing": {"mode": "VOL_RISK", "atr_n": 2.5}}, "sizing.atr_n"),
        ({"exits": {"stop_loss_pct": 100}}, "exits.stop_loss_pct"),
        ({"exits": {"take_profit_pct": -1}}, "exits.take_profit_pct"),
        ({"exits": {"max_hold_bars": 0}}, "exits.max_hold_bars"),
        ({"exits": {"atr_stop": {"n": 14}}}, "exits.atr_stop.mult"),
        ({"exits": {"ibguard": {"tp_pct": 0, "sl_pct": 5}}}, "exits.ibguard.tp_pct"),
        ({"exits": {"ibguard": {"tp_pct": 1001, "sl_pct": 5}}}, "exits.ibguard.tp_pct"),
        ({"exits": {"ibguard": {"tp_pct": 15, "sl_pct": 100}}}, "exits.ibguard.sl_pct"),
        ({"price_mode": "RAW"}, "price_mode"),
        ({"allow_short": 1}, "allow_short"),
        ({"initial_capital": True}, "initial_capital"),
        ({"initial_capital": float("nan")}, "initial_capital"),
        ({"initial_capital": 0}, "initial_capital"),
        ({"max_position_pct": 150}, "max_position_pct"),
        ({"costs": {"short_fee_pct": 0.08}}, "costs.short_fee_pct"),
        ({"costs": {"commission": {"per_share": -1}}}, "costs.commission.per_share"),
        ({"costs": {"commission": {"rate_pct": 0.1}}}, "costs.commission.rate_pct"),
        ({"costs": {"dividend_withholding_pct": 101}}, "costs.dividend_withholding_pct"),
        ({"benchmark": "spy!"}, "benchmark"),
    ],
)
def test_invalid_fields_are_named_by_their_path(changes, path):
    with pytest.raises(RunConfigError) as caught:
        validate_config(config(**changes))
    assert path in [found for found, _ in caught.value.errors], caught.value.errors


def test_every_error_is_reported_and_required_fields_are_named():
    with pytest.raises(RunConfigError) as caught:
        validate_config({"market": "US", "symbols": ["1X"], "price_mode": "RAW"})
    assert {path for path, _ in caught.value.errors} == {"name", "symbols[0]", "start", "strategy", "price_mode"}
    with pytest.raises(RunConfigError):
        validate_config([1, 2])


def test_the_markets_defaults_fill_the_configuration():
    us = validate_config(config())
    assert us.document == {
        "name": "MA cross", "market": "US", "symbols": ["AAA", "BBB"], "list_id": None, "start": "2019-09-03", "end": None,
        "timeframe": "1d",
        "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 20, "slow": 50, "ma": "SMA", "allow_short": False}},
        "sizing": {"mode": "FIXED_FRACTION", "fraction": None},
        "exits": {"stop_loss_pct": None, "take_profit_pct": None, "trailing_pct": None, "atr_stop": None,
                  "max_hold_bars": None, "ibguard": None},
        "price_mode": "REALISTIC", "fill_timing": "NEXT_OPEN", "intrabar_path": "DAILY", "allow_short": False,
        "board_lots_only": False, "initial_capital": 100_000.0, "max_position_pct": 100.0, "benchmark": "SPY",
        "costs": {"commission": {"per_share": 0.005, "min": 1.0, "max_pct": 1.0}, "slippage_bps": 5.0,
                  "dividend_withholding_pct": 30.0, "borrow_fee_pct": 0.5},
        "intraday": None,
    }
    tw = validate_config(config(market="TW", symbols=["2330"], benchmark="0050"))
    assert tw.document["costs"] == {"commission": {"rate_pct": 0.08, "min": 80.0}, "slippage_bps": 10.0,
                                    "dividend_withholding_pct": 0.0, "short_fee_pct": 0.08}
    assert (tw.initial_capital, tw.benchmark) == (1_000_000.0, "0050")


def test_a_complete_configuration_survives_validation_unchanged():
    first = validate_config(config(exits={"atr_stop": {"n": 14, "mult": 2}, "ibguard": {"tp_pct": 15, "sl_pct": 5}},
                                   sizing={"mode": "VOL_RISK", "risk_pct": 0.5}))
    assert validate_config(first.document).document == first.document
    assert (first.exits.atr_n, first.exits.ibguard_tp_pct, first.sizing.atr_n) == (14, 15, 20)


# ----- a run -----


def test_a_run_from_the_store_is_saved_and_read_back(store):
    result = run(store, exits={"stop_loss_pct": 5})
    assert result.flags == {"SURVIVORSHIP": None}
    assert result.time_unit == "day" and set(result.series) == {"TIME", "EQUITY", "BENCHMARK", "CASH", "GROSS", "NET", "POSITIONS"}
    assert result.series["TIME"][0] == to_day(date(2019, 9, 3))
    assert result.data_stamp == {"AAA": {"bar": "2020-12-31", "action": "2020-07-14"}, "BBB": {"bar": "2020-12-31", "action": None}}
    assert {trade.exit_reason for trade in result.trades} <= {"SIGNAL", "SL", "OPEN"} and result.trades
    assert set(result.metrics) == {"strategy", "benchmark"} and result.metrics["strategy"]["beta"] is not None
    saved = save_result(store, result, NOW)
    row = store.backtest(saved)
    assert (row.name, row.market, row.timeframe, row.config, row.metrics, row.tables, row.flags, row.data_stamp) == (
        "MA cross", "US", "1d", result.config, result.metrics, result.tables, result.flags, result.data_stamp)
    series = store.backtest_series(saved)
    for kind, values in result.series.items():
        assert series[kind].tobytes() == values.tobytes(), kind
    assert store.backtest_trades(saved) == list(result.trades)
    assert store.backtest_trades(saved, offset=1, limit=2) == list(result.trades[1:3])


def test_the_csv_exports_have_their_columns_and_a_bom(store):
    result = run(store)
    trades = trades_csv(result.trades, "US", "day")
    equity = equity_csv(result.series, "US", "day")
    assert trades.startswith(b"\xef\xbb\xbf") and equity.startswith(b"\xef\xbb\xbf")
    trade_lines = trades.decode("utf-8-sig").splitlines()
    assert trade_lines[0].split(",") == list(TRADES_COLUMNS)
    assert len(trade_lines) == len(result.trades) + 1
    first = result.trades[0]
    assert trade_lines[1].split(",")[:4] == [first.symbol, first.side, repr(first.quantity), "2019-09-04"]
    equity_lines = equity.decode("utf-8-sig").splitlines()
    assert equity_lines[0].split(",") == list(EQUITY_COLUMNS)
    assert equity_lines[1].split(",")[0] == "2019-09-03" and len(equity_lines) == len(result.series["TIME"]) + 1


def test_minute_timestamps_in_csv_use_the_markets_time_zone():
    entry = int(datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc).timestamp())
    trade = Trade("AAA", "LONG", 1, entry, 10.0, entry + 3600, 11.0, 1.0, 10.0, 0.0, "SIGNAL")
    line = trades_csv((trade,), "US", "second").decode("utf-8-sig").splitlines()[1]
    assert "2026-07-02T09:30:00-04:00" in line and "2026-07-02T10:30:00-04:00" in line


def test_the_benchmark_buys_and_holds_its_symbol(store):
    result = run(store, fill_timing="SAME_CLOSE", costs={"commission": {"per_share": 0, "min": 0, "max_pct": 1},
                                                          "slippage_bps": 0, "dividend_withholding_pct": 0})
    spy = store.daily_bars("US", "SPY")
    start = int(np.searchsorted(spy.day, to_day(date(2019, 9, 3))))
    quantity = np.trunc(100_000 / spy.close[start])
    expected = 100_000 - quantity * spy.close[start] + quantity * spy.close[start:]
    assert np.allclose(result.series["BENCHMARK"], expected)
    assert "BENCHMARK_PARTIAL" not in result.flags
    assert result.metrics["benchmark"]["trades"] == 0 and result.metrics["benchmark"]["open_trades"] == 1


def test_a_benchmark_that_starts_late_is_partial(store):
    late = WEEKDAYS[WEEKDAYS >= to_day(date(2020, 1, 2))]
    put_bars(store, "US", "QQQ", late, walk(7, len(late)))
    result = run(store, benchmark="QQQ")
    assert "BENCHMARK_PARTIAL" in result.flags
    benchmark = result.series["BENCHMARK"]
    first = int(np.flatnonzero(np.isfinite(benchmark))[0])
    assert result.series["TIME"][first] == late[0] and np.isnan(benchmark[:first]).all()
    assert result.metrics["benchmark"]["total_return"] is not None


def test_two_identical_runs_give_identical_results(store):
    first, second = run(store, exits={"trailing_pct": 3}), run(store, exits={"trailing_pct": 3})
    for kind in first.series:
        assert first.series[kind].tobytes() == second.series[kind].tobytes()
    assert (first.trades, first.metrics, first.tables, first.flags) == (second.trades, second.metrics, second.tables, second.flags)


def test_the_warm_up_precedes_the_start(store):
    data = load_run(store, None, validate_config(config(start="2020-03-01")))  # a Sunday: trading starts on Monday
    begin = int(np.searchsorted(WEEKDAYS, to_day(date(2020, 3, 2))))
    assert data.start == 300
    assert data.aligned.days[data.start] == to_day(date(2020, 3, 2)) and data.aligned.days[0] == WEEKDAYS[begin - 300]
    early = load_run(store, None, validate_config(config()))  # fewer than 300 stored bars before the start
    assert early.aligned.days[0] == WEEKDAYS[0] and early.aligned.days[early.start] == to_day(date(2019, 9, 3))


def test_a_period_without_bars_is_no_data(store):
    with pytest.raises(RunError) as caught:
        run(store, start="2021-02-01")
    assert caught.value.code == "NO_DATA"
    with pytest.raises(RunError):
        run(store, symbols=["ZZZ"])


def test_a_tw_run_pays_transaction_taxes(store):
    result = run(store, market="TW", symbols=["2330"], benchmark="0050", costs={"commission": {"rate_pct": 0, "min": 0},
                                                                                 "slippage_bps": 0})
    closed = [trade for trade in result.trades if trade.exit_reason != "OPEN"]
    assert closed and all(trade.costs == pytest.approx(trade.quantity * trade.exit_price * 0.003) for trade in closed)


def test_the_minute_path_without_minute_bars_falls_back(store):
    result = run(store, intrabar_path="MINUTE", exits={"stop_loss_pct": 5})
    assert result.flags["PATH_FALLBACK"] > 0


def test_a_builtin_runs_from_its_configuration(store):
    validated = validate_config(config(strategy={"source": "builtin", "id": "MA_CROSS", "params": {"fast": 10, "slow": 30}}))
    result = run_backtest(validated, load_run(store, None, validated), TAXES, strategy_factory(validated))
    expected = run(store)  # the test's own MA cross of 10 and 30 bars
    assert result.series["EQUITY"].tobytes() == expected.series["EQUITY"].tobytes()
    assert result.config["strategy"]["params"] == {"fast": 10, "slow": 30, "ma": "SMA", "allow_short": False}
