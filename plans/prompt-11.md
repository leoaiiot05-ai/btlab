# Prompt 11: Intraday backtests

Covers SPEC.md §8.3, §8.4, §10.8 (minute timelines) and §11. Acceptance: AC-15.

## 1. Steps

1. **`btlab/intraday.py`** (pure):
   - **Resampling (§11.1).** `resample(bars, opens, minutes)`: one symbol's 1-minute bars, with each bar's session open, into N-minute bars in buckets aligned to the open (bucket k covers `[open + k·N, open + (k + 1)·N)`), each stamped with its bucket's start: the first open, the highest high, the lowest low, the last close and the summed volume. Missing minutes are skipped, an empty bucket gives no bar, and a session's last bucket may be shorter. 1m is the identity.
   - **The timeline (§11.2).** `minute_timeline(market, timeframe, daily, minutes, covered, start_day, close_out, close_out_minutes) -> MinuteRun`:
     - its sessions are the days of the period, warm-up included, on which at least one symbol has coverage OK; its bars are the union of every symbol's bar times in those sessions;
     - each symbol's bars come only from its sessions with coverage OK; at a time without a bar of its own its close is carried (open, high, low and close) with volume 0, and it is not tradable; before its first bar it is NaN; a symbol without coverage for a session is not tradable during it;
     - adjusted prices are raw × the session day's multiplier from the daily `Aligned` (§6.3); the session's corporate actions (cash and ratio) sit on its first bar (an ex-date without a session moves to the next session); the daily limit lock of the session's day applies to all its bars (§6.5);
     - the session's end is the end of its last stored minute bar of any symbol, or the regular close (§4.2); a bar ends at its bucket's end or the session's end, whichever is first;
     - `start` is the first bar of the first session on or after the run's start.
   - `MinuteRun` holds the engine's `Timeline` (with `closeout`, below), the bars `real` mask, the minutes from the open to each bar's end, the session ends, the daily bars of the previous session for each bar (§8.4, adjusted), the bars per regular session (for VOLATILITY's P = 252 × bars per session, §8.1) and the minute paths of its bars (`TimelinePaths`, the exits' `MinutePaths` for minute timelines, §10.8).
   - **Close-out (§11.4).** With SESSION_END and m minutes, the close-out bar of a session is its last bar that ends at least m minutes before the session's end; `closeout` marks it 1 and the session's later bars 2.
   - **The PDT flag (§11.6).** `pdt_windows(trades, timeline, equity_at_session_end)`: day trades are trades opened and closed in one session; a window of 5 consecutive sessions counts when it holds 4 or more day trades on sessions before 2026-06-04 and the run's session-end equity is below USD 25,000 at some session of the window. US only; the flag carries the number of such windows.
2. **`btlab/engine.py`**: `Timeline.closeout` (None on the daily timeline). At the close of a close-out bar and of the session's later bars, every position closes at that close with exit reason CLOSE_OUT, whatever the fill timing, and the pending orders are dropped, so no order carries into the next session (§11.3: "unless close-out applies"). Increasing legs never fill after the close-out bar's open in its session (§11.4).
3. **`btlab/builtins.py`**: `View` gains optional `times`, `session` and `daily` (minute timelines); `plugins.Bars` exposes them.
4. **`btlab/backtest.py`**:
   - `TIMEFRAMES` adds 1m, 5m, 15m, 30m and 60m; `intraday` is null on the daily timeline and `{"close_out": "NONE" | "SESSION_END", "close_out_minutes": 0..120}` on minute ones (defaults NONE and 0).
   - **Loading:** daily data as before (300 bars of warm-up for the daily indicators of §8.4), plus every symbol's 1-minute bars and coverage from the 5 covered sessions before the start (§10.1); a symbol without any coverage OK in the run's period fails the run with `RunError("NO_MINUTE_DATA")`, whose message and `symbols` list them (§11.2).
   - **Running:** the engine on the minute timeline, with the overlays walking each bar's 1-minute bars; the result's curve, cash, exposure and positions are taken at each session's end (§11.7: metrics on session-end equity), `TIME` holds those bars' UTC epoch seconds and `time_unit` is "second"; trades carry epoch seconds. The benchmark is BUY_HOLD on the daily bars of the run's session days, so its curve sits on the same session ends. PDT is added for US runs.
5. **Tests** (`tests/test_intraday.py`, AC-15), hand-made minute bars of 2 symbols over a few sessions:
   - resampling to 5, 15, 30 and 60 minutes, aligned to the open, with a short last bucket and a gap;
   - the timeline: the union of bar times, carried closes, a symbol without coverage for a session, sessions without any coverage left out, adjusted prices, actions on the first bar of the ex-date's session, the session ends of an early close;
   - fills at the next bar within a session and at the next session's first bar;
   - close-out with m = 0 and m = 30, and no entry after it in the session;
   - a TW round trip in one session pays the day-trade tax;
   - the PDT flag: 4 day trades in 5 sessions before 2026-06-04 below USD 25,000 sets it, with its count; after that day, above USD 25,000, or with 3 day trades it does not; never in TW;
   - NO_MINUTE_DATA listing the symbols;
   - metrics on session-end equity through a full minute run from temporary stores, with the series in seconds;
   - a minute bar's path for the overlays, and a plugin's `bars` on a minute timeline (times, session, daily).
6. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **The daily limit lock** of a session's day applies to every bar of that session: §6.5 defines the lock on daily bars.
- **"While the run's equity is below USD 25,000"** is read as: below at the end of at least one session of the window.
- **A position that close-out cannot close** (an untradable bar) gets its CLOSE_OUT order again at every later close of the session; one still open after the session's last bar is closed by the next session's close-out.
- **The benchmark of a minute run** runs on daily bars: the benchmark symbol need not have minute bars, and its session-end values are its daily closes.
- **Built-ins on minute timelines** read the minute bars as they would daily ones; their periods (DCA, rotations) still begin at the first bar of a new month or week.

## 2. Extension points

- **A timeframe** is one entry in `TIMEFRAMES` and a bucket length.
- **Minute paths** for another timeline are another `MinutePaths`.

## 3. Refactor seams

- Resampling, the timeline and the PDT count are pure functions of arrays; backtest.py only loads and assembles.
- The engine knows close-out only through `Timeline.closeout`.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-15 holds.** `.venv/bin/python -m pytest -q`: 873 passed (also with `-X dev -W error`). `intraday` joined the pure modules of `tests/test_purity.py`.
- Built as planned. `minute_timeline` returns a `MinuteRun`; its `paths(adjusted)` gives the overlays each bar's 1-minute bars (`TimelinePaths`).
- The engine's close-out is `Timeline.closeout` only: at a marked bar's close every position gets a CLOSE_OUT order filled at that close, and the pending orders are replaced, so no entry fills later in the session and nothing carries over.
- Adjusted minute volume uses the session day's daily ratio of adjusted to raw volume.
- **For prompts 12–14:** `RunConfig.intraday` holds `close_out` and `close_out_minutes`; `RunError.symbols` lists the symbols of NO_MINUTE_DATA; `minute_view(minute, data)` builds a strategy's `View` on a minute timeline (`times`, `session`, `daily`), and `plugins._cut` truncates every array of a `View`, so the look-ahead check works on both timelines.
