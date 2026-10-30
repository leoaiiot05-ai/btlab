"""Minute timelines: resampling, fills, close-out, day trades, PDT and minute runs (SPEC.md §11, AC-15)."""

from datetime import date

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from btlab.backtest import RunError, load_run, minute_view, run_backtest, strategy_factory, validate_config
from btlab.calendar import session
from btlab.engine import simulate
from btlab.exits import ExitRules, Overlays
from btlab.intraday import minute_timeline, pdt_windows, resample, session_end_bars
from btlab.plugins import Bars
from btlab.prices import align
from btlab.records import Action, DailyBars, MinuteBars, MinuteCoverage, Trade, to_day
from btlab.store import MinuteStore
from builders import NOW, TAXES, Script, put_bars, settings, synthetic_store, tw_costs, us_costs

MON = to_day(date(2026, 3, 2))
DAYS = np.array([MON, MON + 1, MON + 2])  # Monday to Wednesday


def minutes_of(market: str, day: int, offsets, closes, volume: float = 100.0) -> MinuteBars:
    """1-minute bars at minute offsets from the session's open, each opening at its close − 0.1."""
    open_ = session(market, int(day))[0]
    close = np.array(closes, dtype=np.float64)
    return MinuteBars(np.array([open_ + 60 * m for m in offsets], dtype=np.int64), close - 0.1, close + 0.2, close - 0.2,
                      close, np.full(len(close), volume))


def joined(*parts: MinuteBars) -> MinuteBars:
    return MinuteBars(*(np.concatenate([getattr(p, name) for p in parts]) for name in ("t", "open", "high", "low", "close", "volume")))


def daily_for(market: str, symbols: dict, days=DAYS, actions=None):
    bars = {symbol: DailyBars(days, np.full(len(days), close), np.full(len(days), close + 5), np.full(len(days), close - 5),
                              np.full(len(days), close), np.full(len(days), 1e6), np.zeros(len(days), dtype=bool))
            for symbol, close in symbols.items()}
    aligned, _ = align(market, days, {symbol: (bars[symbol], (actions or {}).get(symbol, [])) for symbol in symbols})
    return aligned


# ----- resampling (§11.1) -----

OFFSETS = [0, 1, 2, 4, 6, 14, 15, 29, 59, 61, 389]


@pytest.mark.parametrize(
    "minutes, starts",
    [(1, OFFSETS), (5, [0, 5, 10, 15, 25, 55, 60, 385]), (15, [0, 15, 45, 60, 375]), (30, [0, 30, 60, 360]),
     (60, [0, 60, 360])],
)
def test_resampling_aligns_buckets_to_the_open(minutes, starts):
    one = minutes_of("US", MON, OFFSETS, [10.0 + i for i in range(len(OFFSETS))])
    open_ = session("US", MON)[0]
    built = resample(one, np.full(len(one.t), open_), minutes)
    assert ((built.t - open_) // 60).tolist() == starts


def test_a_built_bar_takes_first_open_high_low_last_close_and_summed_volume():
    one = minutes_of("US", MON, [0, 1, 4, 6], [10.0, 12.0, 9.0, 11.0], volume=50.0)
    open_ = session("US", MON)[0]
    built = resample(one, np.full(4, open_), 5)
    assert built.open.tolist() == pytest.approx([9.9, 10.9])
    assert built.high.tolist() == pytest.approx([12.2, 11.2])
    assert built.low.tolist() == pytest.approx([8.8, 10.8])
    assert built.close.tolist() == [9.0, 11.0]
    assert built.volume.tolist() == [150.0, 50.0]


def test_the_last_bucket_of_an_early_close_is_short():
    early = to_day(date(2026, 11, 27))  # closes at 13:00 New York
    one = minutes_of("US", early, [0, 200, 209], [10.0, 11.0, 12.0])
    daily = daily_for("US", {"A": 10.0}, days=np.array([early]))
    run = minute_timeline("US", "60m", daily, [one], [np.array([early])], early)
    open_ = session("US", early)[0]
    assert ((run.timeline.time - open_) // 60).tolist() == [0, 180]
    assert run.session_ends.tolist() == [open_ + 210 * 60]
    assert run.minutes.tolist() == [60.0, 210.0]  # the last bar ends with the session


# ----- the timeline (§11.2) -----


def two_symbol_run(timeframe="5m", **options):
    a = joined(*(minutes_of("US", day, [0, 1, 5, 7, 385, 389], [100 + k, 101 + k, 102 + k, 103 + k, 104 + k, 105 + k])
                 for k, day in enumerate(DAYS)))
    b = joined(*(minutes_of("US", day, [0, 6, 389], [50 + k, 51 + k, 52 + k]) for k, day in enumerate(DAYS)))
    daily = daily_for("US", {"A": 100.0, "B": 50.0}, actions=options.pop("actions", None))
    covered = options.pop("covered", [DAYS, DAYS[[0, 2]]])
    return minute_timeline("US", timeframe, daily, [a, b], covered, int(DAYS[0]), **options)


def test_the_timeline_carries_closes_and_marks_uncovered_sessions_untradable():
    run = two_symbol_run()
    timeline = run.timeline
    open_ = session("US", MON)[0]
    assert ((timeline.time[:4] - open_) // 60).tolist() == [0, 5, 385, 24 * 60 + 0]
    assert timeline.session.tolist() == [0, 0, 0, 1, 1, 1, 2, 2, 2]
    # B has no bar at 5 minutes on Monday (its 6-minute bar falls in that bucket: it has one), but no coverage on Tuesday.
    assert run.real[:, 1].tolist() == [True, True, True, False, False, False, True, True, True]
    assert timeline.tradable[:, 1].tolist() == [True, True, True, False, False, False, True, True, True]
    assert_allclose(timeline.close[3:6, 1], [52.0, 52.0, 52.0])  # carried from Monday's last close
    assert_array_equal(timeline.day, np.repeat(DAYS, 3))


def test_sessions_without_any_coverage_are_left_out_and_actions_sit_on_a_sessions_first_bar():
    actions = {"A": [Action(int(MON + 1), "SHARE_RATIO", "SPLIT", ratio=2.0)]}
    run = two_symbol_run(covered=[DAYS[[0, 2]], DAYS[[0, 2]]], actions=actions)
    assert run.session_days.tolist() == [MON, MON + 2]
    ratio = run.timeline.ratio[:, 0]
    assert ratio.tolist() == [1, 1, 1, 2, 1, 1]  # Tuesday's split moves to Wednesday, the next session
    assert_allclose(run.timeline.adj_close[0, 0] / run.timeline.close[0, 0], 0.5)


def test_minute_paths_are_the_bars_one_minute_bars():
    run = two_symbol_run()
    paths = run.paths(adjusted=False)
    opens, highs, lows, closes = paths.bars(0, 0)
    assert closes.tolist() == [100.0, 101.0] and paths.bars(1, 0)[3].tolist() == [102.0, 103.0]
    assert paths.bars(3, 1) is None  # B had no coverage on Tuesday


# ----- fills, close-out and day trades (§11.3 to §11.5) -----


def test_next_open_fills_at_the_next_bar_and_across_sessions():
    run = two_symbol_run()
    script = Script({0: [("order_target_qty", 0, 10)], 2: [("order_target_qty", 0, 0)]})
    result = simulate(run.timeline, script, settings(us_costs(2, borrow=0.0)))
    buy, sell = result.fills
    assert buy.time == run.timeline.time[1] and buy.price == pytest.approx(run.timeline.open[1, 0])
    assert sell.time == run.timeline.time[3] and sell.price == pytest.approx(run.timeline.open[3, 0])  # Tuesday's first bar


@pytest.mark.parametrize("minutes, closeout_bar, marks", [(0, 2, [0, 0, 1]), (30, 1, [0, 1, 2])])
def test_close_out_closes_every_position_m_minutes_before_the_end(minutes, closeout_bar, marks):
    # Monday's bars end 5, 10 and 390 minutes after the open, which is also the session's end.
    run = two_symbol_run(close_out="SESSION_END", close_out_minutes=minutes)
    assert run.timeline.closeout[:3].tolist() == marks
    script = Script({0: [("order_target_qty", 0, 10)], 2: [("order_target_qty", 0, 20)]})
    result = simulate(run.timeline, script, settings(us_costs(2, borrow=0.0)))
    close = [fill for fill in result.fills if fill.reason == "CLOSE_OUT"][0]
    assert close.time == run.timeline.time[closeout_bar] and close.price == pytest.approx(run.timeline.close[closeout_bar, 0])
    # An order placed after the close-out bar never fills: not later that session, not the next morning.
    assert [fill.time for fill in result.fills if fill.side == "BUY"] == [run.timeline.time[1]]
    assert result.quantity[closeout_bar - run.timeline.start:, 0].tolist() == [0.0] * (9 - closeout_bar)


def test_a_tw_round_trip_in_one_session_pays_the_day_trade_tax():
    market_days = np.array([MON, MON + 1])
    stock = joined(*(minutes_of("TW", day, [0, 1, 2, 3], [600.0, 601.0, 602.0, 603.0]) for day in market_days))
    daily = daily_for("TW", {"2330": 600.0}, days=market_days)
    run = minute_timeline("TW", "1m", daily, [stock], [market_days], int(market_days[0]))
    script = Script({0: [("order_target_qty", 0, 1_000)], 1: [("order_target_qty", 0, 0)],
                     4: [("order_target_qty", 0, 1_000)], 6: [("order_target_qty", 0, 0)]})
    result = simulate(run.timeline, script, settings(tw_costs(), "TW", 10_000_000.0))
    sells = [fill for fill in result.fills if fill.side == "SELL"]
    assert sells[0].costs == pytest.approx(1_000 * sells[0].price * 0.0015)  # bought and sold the same session
    assert sells[1].costs == pytest.approx(1_000 * sells[1].price * 0.0015)


# ----- the PDT flag (§11.6) -----


def day_trades(times: np.ndarray, sessions_with: list[int], per_session: list[int]) -> list[Trade]:
    found = []
    for session_index, count in zip(sessions_with, per_session):
        for _ in range(count):
            moment = int(times[session_index * 2])
            found.append(Trade("A", "LONG", 1, moment, 10.0, moment + 60, 11.0, 1.0, 10.0, 0.0, "SIGNAL"))
    return found


@pytest.mark.parametrize(
    "first_day, equity, trades, windows",
    [
        # One day trade in each of sessions 0 to 3: only the window of sessions 0 to 4 holds four.
        (date(2026, 5, 4), 10_000.0, [1, 1, 1, 1, 0, 0], 1),
        (date(2026, 5, 4), 30_000.0, [1, 1, 1, 1, 0, 0], 0),
        (date(2026, 5, 4), 10_000.0, [1, 1, 1, 0, 0, 0], 0),
        (date(2026, 6, 8), 10_000.0, [1, 1, 1, 1, 0, 0], 0),
    ],
    ids=["below 25,000", "above 25,000", "three day trades", "after 2026-06-04"],
)
def test_the_pdt_count(first_day, equity, trades, windows):
    session_days = np.array([to_day(first_day) + k for k in range(6)])
    sessions = np.repeat(np.arange(6), 2)
    times = np.arange(12) * 3_600
    found = pdt_windows(day_trades(times, list(range(6)), trades), times, sessions, session_days, np.full(6, equity), 0)
    assert found == windows


def test_pdt_needs_equity_below_25000_in_some_session_of_the_window():
    session_days = np.array([to_day(date(2026, 5, 4)) + k for k in range(5)])
    sessions, times = np.repeat(np.arange(5), 2), np.arange(10) * 3_600
    trades = day_trades(times, [0, 1, 2, 3], [1, 1, 1, 1])
    assert pdt_windows(trades, times, sessions, session_days, np.array([30e3, 30e3, 24_999.0, 30e3, 30e3]), 0) == 1
    assert pdt_windows(trades, times, sessions, session_days, np.array([30e3, 30e3, 25_000.0, 30e3, 30e3]), 0) == 0


def test_pdt_counts_every_window_and_ignores_trades_across_sessions():
    days = np.array([to_day(date(2026, 5, 4)) + k for k in range(7)])
    sessions = np.repeat(np.arange(7), 2)
    times = np.arange(14) * 3_600
    trades = day_trades(times, [1, 2, 3, 4], [1, 1, 1, 1])
    assert pdt_windows(trades, times, sessions, days, np.full(7, 10_000.0), 0) == 2  # windows from sessions 0 and 1
    overnight = [Trade("A", "LONG", 1, int(times[1]), 10.0, int(times[2]), 11.0, 1.0, 10.0, 0.0, "SIGNAL")] * 4
    assert pdt_windows(overnight, times, sessions, days, np.full(7, 10_000.0), 0) == 0


# ----- minute runs through the stores -----


@pytest.fixture
def stores(tmp_path):
    daily = synthetic_store(tmp_path)
    minute = MinuteStore(tmp_path)
    days = [int(day) for day in daily.daily_bars("US", "SPY").day[-6:]]
    for k, day in enumerate(days):
        for symbol, base in (("AAA", 100.0), ("BBB", 50.0)):
            bars = minutes_of("US", day, range(0, 390, 1), base + k + 0.01 * np.arange(390))
            minute.save_minute_day("US", symbol, MinuteCoverage(day, "OK", 1.0, NOW), bars)
    yield daily, minute, days
    daily.close()
    minute.close()


def minute_config(days, **changes) -> dict:
    return {"name": "minutes", "market": "US", "symbols": ["AAA", "BBB"], "start": date.fromordinal(date(1970, 1, 1).toordinal() + days[3]).isoformat(),
            "timeframe": "15m", "strategy": {"source": "builtin", "id": "MA_CROSS", "params": {"fast": 2, "slow": 5}},
            "intraday": {"close_out": "SESSION_END", "close_out_minutes": 0}, **changes}


def test_a_minute_run_measures_session_ends(stores):
    daily, minute, days = stores
    config = validate_config(minute_config(days))
    data = load_run(daily, minute, config)
    result = run_backtest(config, data, TAXES, strategy_factory(config))
    assert result.time_unit == "second"
    open_close = [session("US", day) for day in days[3:]]
    assert result.series["TIME"].tolist() == [close - 15 * 60 for _, close in open_close]
    assert len(result.series["EQUITY"]) == 3 and result.metrics["strategy"]["total_return"] is not None
    assert all(trade.exit_reason in ("SIGNAL", "CLOSE_OUT") for trade in result.trades) and result.trades
    assert all(open_close[0][0] <= trade.entry_time for trade in result.trades)  # minute timestamps
    assert np.isfinite(result.series["BENCHMARK"]).all()


def test_a_minute_run_without_minute_bars_for_a_symbol_fails(stores):
    daily, minute, days = stores
    put_bars(daily, "US", "CCC", daily.daily_bars("US", "SPY").day, np.full(len(daily.daily_bars("US", "SPY").day), 20.0))
    config = validate_config(minute_config(days, symbols=["AAA", "CCC"]))
    with pytest.raises(RunError) as caught:
        load_run(daily, minute, config)
    assert (caught.value.code, caught.value.symbols) == ("NO_MINUTE_DATA", ("CCC",))
    assert "CCC" in str(caught.value)


def test_the_warm_up_takes_five_covered_sessions(stores):
    daily, minute, days = stores
    config = validate_config(minute_config(days, start=date.fromordinal(date(1970, 1, 1).toordinal() + days[5]).isoformat()))
    data = load_run(daily, minute, config)
    run = minute_timeline("US", "15m", data.aligned, data.minutes, data.covered, config.start)
    assert run.session_days.tolist() == days  # five sessions before the start, and the start's own
    assert run.timeline.day[run.timeline.start] == days[5]


def test_a_plugins_bars_on_a_minute_timeline(stores):
    daily, minute, days = stores
    config = validate_config(minute_config(days))
    data = load_run(daily, minute, config)
    run = minute_timeline("US", "15m", data.aligned, data.minutes, data.covered, config.start)
    bars = Bars(minute_view(run, data))
    assert_array_equal(bars.times, run.timeline.time)
    assert_array_equal(bars.session, run.timeline.session)
    previous = np.searchsorted(data.aligned.days, run.timeline.day) - 1
    assert_allclose(bars.daily.close[:, 0], data.aligned.adj_close[previous, 0])


def test_overlays_walk_a_bars_one_minute_bars():
    run = two_symbol_run()
    script = Script({0: [("order_target_qty", 0, 10)]})
    result = simulate(run.timeline, script, settings(us_costs(2, borrow=0.0)),
                      Overlays(ExitRules(take_profit_pct=1.5), run.paths(adjusted=False)))
    sell = result.fills[-1]
    # Entered at bar 1's open, 101.9: its 1-minute bars top out at 103.2, below 101.9 × 1.015 = 103.43. Bar 2's first
    # 1-minute bar opens at 103.9 after a gap, so the take-profit fills there.
    assert (sell.reason, sell.time, sell.price) == ("TP", run.timeline.time[2], pytest.approx(103.9))


def test_session_end_bars():
    assert session_end_bars(np.array([0, 0, 1, 1, 1, 2]), 1).tolist() == [1, 4, 5]


def test_the_pdt_flag_of_a_us_minute_run(stores):
    daily, minute, days = stores
    # A 1-minute MA cross with close-out trades every session; a small account before 2026-06-04 is flagged.
    config = validate_config(minute_config(days, timeframe="1m", initial_capital=10_000.0,
                                           start=date.fromordinal(date(1970, 1, 1).toordinal() + days[0]).isoformat()))
    result = run_backtest(config, load_run(daily, minute, config), TAXES, strategy_factory(config))
    assert date.fromordinal(date(1970, 1, 1).toordinal() + days[-1]) < date(2026, 6, 4)
    assert result.flags.get("PDT", 0) >= 1
