# btlab

btlab is your personal backtesting lab. It runs as one Python program on your Windows 11 PC and you use it through a web page: in Edge on the PC, and in Safari on your iPhone and iPad through Tailscale.

This README takes you from a clean PC to a first backtest and a test-mode live portfolio. `SPEC.md` is the full specification; the section numbers in brackets (§) point into it.

## 1. What btlab does, and what it does not

btlab:

- downloads daily prices and corporate actions (dividends, splits) for US stocks and ETFs from **Tiingo** and for Taiwan stocks and ETFs from **FinMind**, the 3-month T-bill rate from **FRED**, and, for symbols you choose, 1-minute bars from **Interactive Brokers** (IB);
- backtests strategies on one market at a time (US or Taiwan): ten built-in strategies, your own Python plugins, and rules you click together in a rule builder; on daily or minute bars, long and short, with exits, position sizing, commissions, taxes, slippage and corporate actions;
- shows metrics, tables and charts, stores every run, and compares runs;
- optimizes parameters: a grid search, an out-of-sample check and walk-forward analysis;
- runs one **live portfolio** per market that follows a saved daily backtest. In **test mode** it proposes orders, you confirm them, and it fills them virtually on the next session's real prices, so you get a paper-trading curve. In **trade mode** it sends the orders you confirm to IB;
- coordinates with **ibguard**, your exit engine: it writes the take-profit and stop-loss a backtest used, and it lifts and restores ibguard's protection around its own sells;
- imports the leading-indicator series of **spylead** as indicators;
- keeps recently opened results readable offline on the iPhone and iPad.

btlab does not:

- trade by itself: every batch of orders waits for your confirmation, and nothing is sent in test mode;
- execute a strategy's stops or take-profits live: ibguard protects live positions;
- trade minute strategies live (backtests only), mix US and Taiwan symbols in one backtest, or handle futures, options, crypto or currencies;
- know delisted symbols or past index members, so results lean optimistic (see §20, Limitations);
- run in the cloud: the PC must be on, with btlab running, for the iPhone and iPad to reach it.

## 2. What you need

- **Windows 11** on the PC, which stays on while you want btlab to work.
- **Python 3.13, 64-bit**, from python.org, with the `py` launcher (installed by default). The committed `pylock.toml` was made with Python 3.13 on Windows; another version needs a new lock.
- **Git for Windows**.
- **The external drive** at `E:` with the project in `E:\00_CODING\btlab` (see §17 for a fixed drive letter).
- Free accounts at **Tiingo**, **FinMind** and **FRED** (next section).
- Optional: **IB Gateway** for minute bars and trade mode; **ibguard** and **spylead** running on the PC; **Tailscale** for the iPhone and iPad; an SMTP account for emails.

## 3. Keys and tokens

Put each one in `config.toml` (§5). btlab never writes them to a log, an event, an email or an error message.

| Service | How to get it | `config.toml` key |
|---|---|---|
| Tiingo | Sign up free at tiingo.com, then open your account's API page and copy the token. | `[tiingo] api_key` |
| FinMind | Register at finmindtrade.com and **verify your email address**; then copy the API token shown on your account page. | `[finmind] token` |
| FRED | Create an account at fred.stlouisfed.org, then My Account, API Keys, "Request API Key". | `[fred] api_key` |
| spylead | The web token you set in spylead's own config. | `[spylead] token` |
| ibguard | The web token you set in ibguard's own config. | `[ibguard] token` |
| btlab itself | Any random text of at least 16 characters; you type it to log in. Make one with `py -c "import secrets; print(secrets.token_urlsafe(24))"`. | `[web] token` |

Free-plan limits that matter: Tiingo allows 50 requests an hour and 1,000 a day (btlab keeps to 45 and 900) and 500 different symbols a month; FinMind allows 600 requests an hour (btlab keeps to 550) and is down for maintenance on Sundays 00:00 to 07:00 Taipei time.

Licences: Tiingo's data are for internal use, FinMind's for non-commercial use with attribution (the Settings view credits it), FRED's under its terms of use.

## 4. Install

In PowerShell:

```powershell
git clone https://github.com/leoaiiot05-ai/btlab.git E:\00_CODING\btlab
cd E:\00_CODING\btlab
# Only on an exFAT drive: Git refuses folders without an owner until you trust this one.
git config --global --add safe.directory E:/00_CODING/btlab
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install --upgrade pip
.venv/Scripts/python -m pip install -r pylock.toml
```

The last command installs exactly the locked versions with their hashes: NumPy, aiohttp, tzdata, ib_async 2.1.0 and pytest, with their own dependencies. btlab is not installed as a package; it always runs from this folder.

Check the install by running the tests (about a minute; they never touch the network or IB):

```powershell
.venv/Scripts/python -m pytest -q
```

## 5. Configure

```powershell
copy config.example.toml config.toml
notepad config.toml
```

`config.toml` stays on the PC: Git ignores it, and so it must stay. Key by key:

- `mode = "test"`: keep it until §13 (trade mode). Test mode never sends an order.
- `[web]` `port = 8091`; `token`: your login token from §3. btlab listens only on 127.0.0.1, so nothing outside the PC reaches it directly.
- `[tiingo]`, `[finmind]`, `[fred]`: the keys. An empty key only warns at startup; that source then fails with MISSING_KEY.
- `[spylead]`, `[ibguard]`: `url` and `token`. An empty `url` turns the import or the coordination off.
- `[ib]`: `host = "127.0.0.1"` (empty turns IB off: no minute bars, no orders), `port` (4002 is IB Gateway's paper port, 4001 its live port), `client_id = 31` (it must differ from ibguard's 17), `account` (needed only when your IB login has several accounts).
- `[minute] backfill_years = 3`: how far back minute bars go.
- `[orders]`: how live US orders are placed: `us_order_kind = "MOO"` (market on open) or `"LIMIT"`, and the limit offsets in percent for US and Taiwan.
- `[limits]`: commented out; trade mode refuses to start without it (§13).
- `[schedule]`: automatic updates, in Taipei time: US Tuesday to Saturday at `us_update` (08:30), Taiwan Monday to Friday at `tw_update` (18:00). `enabled = false` turns them off.
- `[email]`: optional (§16). An empty `smtp_host` turns email off.

Mistakes are reported at startup with the key's name, never its value. `taxes.toml` (Taiwan's transaction tax by category and date) sits next to `config.toml`; edit it only when the law changes, then restart btlab.

## 6. First run

Double-click `run.bat` in the project folder, or in PowerShell:

```powershell
.venv/Scripts/python -m btlab --config config.toml
```

The console shows the log; `logs\btlab.log` keeps a more detailed one (5 files of 5 MB). Ctrl+C stops btlab cleanly. If the window says another btlab is running (exit code 3), one already is, perhaps from the autostart.

Open `http://127.0.0.1:8091` in Edge and log in with your web token. Then install it as an app: Edge's menu (…), **Apps**, **Install this site as an app**. It opens in its own window and from the Start menu.

The views: **Home** (run mode, data status, jobs, connections, batches waiting, latest events), **Data**, **Backtest**, **Results**, **Optimize**, **Live** and **Settings** (language: 中文 or English; logout; credits; schedule and limits; saved offline results).

## 7. Data: lists, updates and minute bars

### Lists and the first update

1. **Data** view, market **US**: type a list name, **Create list**, then add symbols (`SPY, QQQ, AAPL`). Class shares use a dot: `BRK.B`. Taiwan codes look like `2330` or `0050`.
2. Press **Update US** (and **Update TW** for Taiwan lists). Every symbol of every list of the market is updated, plus SPY (US) or 0050 (Taiwan), which define the calendar and are the default benchmarks.

The first update of a market fixes its data window at 31 years back (30 years plus a year of indicator warm-up) and fetches every symbol over that window:

- **US (Tiingo)**: one request per symbol, at most 45 an hour. About 45 symbols take the first hour; 50 symbols plus SPY finish a few minutes into the second hour. Home shows "waiting for Tiingo's hourly limit until …"; the job waits and can be cancelled. Later updates need one request per symbol and take about a minute.
- **Taiwan (FinMind)**: about 5 requests per symbol (prices, dividend results, dividend policy, capital reductions, short-sale suspensions) plus a few for the whole market; about 250 requests for 50 symbols, within the 550 an hour.
- **FRED**: one request per update.

The symbols table shows each symbol's name, category (STOCK, ETF, BOND_ETF, TDR; you can override it, which matters for Taiwan's taxes), exchange, data range, last bar and last error. A symbol that fails keeps its error there and the update continues (outcome PARTIAL). Removing a symbol from all lists keeps its data; **Purge** deletes it.

### Automatic updates and catch-up

With `[schedule] enabled = true`, the US update runs Tuesday to Saturday at 08:30 Taipei (after the US close), the Taiwan one Monday to Friday at 18:00 (FinMind publishes around 17:30). If the PC was off at that time, the missed update runs once when btlab starts, not once per missed day. A failed scheduled update retries after 30 minutes, at most 3 times, then emails you. FinMind's Sunday maintenance makes the update wait until 07:05.

After each successful update btlab, in order: fills the virtual orders of test-mode live portfolios, queues minute bars for new sessions, and proposes the next live batch (§12). With spylead configured it also takes a spylead snapshot; **Snapshot now** on the Data view takes one at once.

### Minute bars (IB)

Minute bars cost hours of requests, so btlab fetches them only for symbols you switch to **Minute bars** in the symbols table, plus the symbols of active live portfolios.

- They need IB Gateway running and logged in (§15), and `[ib] host` set. Any login works for history, paper or read-only.
- **Start minute backfill** on the Data view fetches `backfill_years` (3) back, newest sessions first, one request per symbol and session, at least 2 seconds apart: 3 years is about 750 sessions, so at least 25 minutes per symbol, often more when IB slows requests. It resumes where it stopped; every data update also queues the new sessions.
- Each symbol-day gets a coverage state: OK, EMPTY (IB had no bars, say a halt), FAILED (retried later), NO_PERMISSION, MISMATCH (prices that do not match the daily bars; that day falls back to the daily path).
- NO_PERMISSION means IB refused the market data; btlab stops asking for that market until its next update. **IB does not offer Taiwan market access to residents of Taiwan**, so Taiwan minute bars may never come; Taiwan backtests then use daily bars.

## 8. Your first backtest

1. **Backtest** view. **Universe**: market US, your list (its symbols fill in), a start date; leave End empty for the latest bar.
2. **Strategy**: Source **Built-in**, strategy **MA_CROSS**, keep fast 20 and slow 50.
3. Leave **Sizing**, **Exits**, **Execution** and **Costs and capital** at their defaults for now; they are explained in §9.
4. **Run backtest**. Wrong fields are marked where they are. The result appears under **Results** after a few seconds.
5. Open it: the flags at the top, the metric cards next to the benchmark's, the equity chart against the benchmark (with a log-scale switch), drawdown, monthly and annual returns, exposure, candles with trade markers for any symbol, and the trades table. Actions: rename, keep offline, export trades or equity as CSV (UTF-8 with BOM, so Excel shows Chinese names), compare with 2 to 4 others, delete, **Use for a live portfolio**, and **Write to ibguard…** when the run used the ibguard rule.

## 9. How a backtest works

### Price modes

- **REALISTIC** (default): signals and indicators use prices adjusted for dividends and splits; fills, valuation and costs use the prices as traded, and dividends and splits are applied to the position on their ex-dates (cash credited after withholding, quantities multiplied, fractional shares paid in cash).
- **ADJUSTED**: everything on adjusted prices. Simpler, but dividends are reinvested untaxed, and lot sizes and minimum commissions act on adjusted prices, which are off in early years.

### Fill timing

- **NEXT_OPEN** (default): a signal at a bar's close fills at the next tradable bar's open. This is what you can really do.
- **SAME_CLOSE**: it fills at the same bar's close. Optimistic; use it only to compare.

Buys pay the price plus slippage, sells receive the price minus slippage. An order that cannot fill (no trade that day, volume 0, or a Taiwan limit-up or limit-down lock in the blocked direction) waits for the next bar that can fill it. At each fill moment sells and covers go before buys; when cash is short, all buys are scaled down by the same factor. Quantities round down to the lot: 1 share, or 1,000 in Taiwan with **Board lots only**.

### Order of events in a bar

At a session's start: corporate actions, then due Taiwan forced covers, then borrow fees. Then pending orders fill at the open. Then exits along the bar's path. At the close: positions are valued, max-hold exits come due, and the strategy decides with data up to that close. Open positions at the end stay open and show as OPEN in the trades.

### Sizing

For strategies that give a direction (long, short, flat), each entry is sized once and held until the direction changes (no rebalancing in between):

- **FIXED_FRACTION** (default): a fraction of equity, 1/N for N symbols when empty;
- **FIXED_AMOUNT**: a currency amount; **FIXED_SHARES**: a share count;
- **VOL_RISK**: risk a percentage of equity against a multiple of the ATR.

Every entry is capped at **Maximum position %** of equity. Weight strategies (buy and hold, rotations) size themselves.

### Exits and intrabar paths

Exits stack and apply to every position, whatever the strategy: **Stop loss %**, **Take profit %**, **Trailing stop %**, **ATR stop**, **Maximum bars held**, and the **ibguard rule** (take-profit and stop-loss measured from a reference that ratchets up as ibguard does live). Levels are for longs and mirrored for shorts.

Within a bar btlab assumes a path:

- **DAILY** path: an up bar goes open, low, high, close; a down bar open, high, low, close.
- **MINUTE** path: the day's 1-minute bars in order, where minute bars exist with coverage OK; other days fall back to the daily path and the result counts them (flag PATH_FALLBACK).

A level crossed between two path points fills at the level; one already passed at a gap (the open, or a jump between minutes) fills at the gap price. When several exits trigger together, the first one reached wins; on a tie, stops beat profit targets. After an exit the position re-enters only when the strategy changes its mind.

### Costs and taxes

Prefilled per market and editable per backtest:

| | US | Taiwan |
|---|---|---|
| Commission | USD 0.005 a share, minimum USD 1, at most 1% of the trade | 0.08% of the trade, minimum TWD 80 (IB's Taiwan rate) |
| Slippage | 5 bps | 10 bps |
| Transaction tax | none | from `taxes.toml` on every sell: stocks 0.3%, day trades 0.15% (2017-04-28 to 2027-12-31), ETFs and TDRs 0.1%, bond ETFs 0% from 2017 to 2026 and 0.1% before and after |
| Dividend withholding | 30% | 0% |
| Shorts | borrow fee 0.5% a year, dividends paid | short-sale fee 0.08%, forced cover at a short-sale suspension |

A Taiwan broker's commission (for example 0.1425% × your discount, minimum TWD 20) can be entered instead of IB's.

### Shorting

Only with **Allow shorts**, and only from strategies that give directions; rotations stay long. Without it, short signals count as flat and the result says SHORTS_IGNORED. A short needs collateral from your own cash: 50% of its value in the US, 90% in Taiwan. There are no margin calls.

### Benchmark

Buy and hold of the benchmark symbol (SPY or 0050 by default), run through the same engine with the same costs and capital.

### Flags

| Flag | Meaning |
|---|---|
| SURVIVORSHIP | Always: lists hold only symbols that still exist, so results are biased upward. |
| LOOKAHEAD_SUSPECT | A vectorized plugin's signals changed when future bars were cut off: it probably peeks ahead. The run still completes; distrust it. |
| IN_SAMPLE_SERIES | The run used spylead's F or COMPOSITE, which are fitted over their whole history: optimistic. |
| SHORTS_IGNORED | The strategy wanted shorts but the run does not allow them. |
| PATH_FALLBACK | Position-days that used the daily path for lack of minute bars. |
| DATA_ENDED | A symbol's data stopped before the run's end; its position stays at its last close. |
| BENCHMARK_PARTIAL | The benchmark has data for only part of the period. |
| PDT | US minute runs: windows with 4 or more day trades in 5 sessions below USD 25,000, before 2026-06-04, when FINRA's pattern-day-trader rule ended. Informational; some brokers keep their own limits until 2027-10-20. |

### Metrics

Total return, CAGR, volatility, Sharpe and Sortino (against the T-bill rate), maximum drawdown and its duration, Calmar, win rate, payoff ratio, profit factor, expectancy, trades, exposure, turnover, alpha, beta and correlation to the benchmark, annual and monthly returns. A value that cannot be computed shows as "—".

### Minute backtests

Pick a minute timeframe (1, 5, 15, 30 or 60 minutes) in the strategy or rule. They need minute bars for the symbols (else NO_MINUTE_DATA). Daily indicators there use the previous session's values, because today's daily bar is unknown until the close. **Close-out** can flatten every position a set number of minutes before the session ends.

## 10. Strategies: built-ins, plugins and the rule builder

### Built-ins

| Strategy | Idea |
|---|---|
| MA_CROSS | Long while the fast average is above the slow one. |
| RSI | Buy when RSI is below `lower`, sell above `upper`. |
| MACD | Long while MACD is above its signal line. |
| BOLLINGER | Buy below the lower band, sell above the middle. |
| BREAKOUT | Buy when the close breaks above the highest high of the previous `entry_n` bars, sell below the lowest low of the previous `exit_n`. |
| BUY_HOLD | Equal weights on the first bar, then hold. |
| DCA | Every month or week, buy a fixed amount split equally across the symbols; never sells. |
| MOMENTUM_ROTATION | Each month or week, hold the top K by momentum, optionally only those above 0. |
| DUAL_MOMENTUM | Each month, the best risk asset if it beats the T-bill, else the safe asset or cash. |
| ZSCORE_REVERSION | Buy when the price is far below its average, sell when it returns. |

The direction strategies take **allow_short** to go short instead of flat.

### Plugins

A plugin is a Python file in `strategies/` whose name does not start with `_`. btlab loads them at start; after adding or changing one, restart btlab. A plugin that fails to load is listed with its error in the Backtest view; the others still load. Plugins run with your own rights, so only put your own code there.

Every plugin defines `NAME_EN`, `NAME_ZH`, `TIMEFRAME` (`"1d"` or a minute timeframe such as `"5m"`), `PARAMS` (each parameter's type, default, range and labels), and one of two functions:

- **Vectorized**, `signals(bars, params)`: returns an array of bars × symbols with target weights (or directions, with `OUTPUT = "direction"`), NaN meaning "no instruction". `bars` offers read-only arrays: `close`, `open`, `high`, `low`, `volume` (adjusted), `tradable`, `times`, `symbols`, `rf`, `daily` (on minute timelines) and `ext(name)` for spylead series. Before each backtest btlab recomputes the signals with the data cut at five points; if anything before a cut changes, the result gets LOOKAHEAD_SUSPECT. See `strategies/example_vector.py` (an MA cross giving equal weights to the symbols in an uptrend).
- **Event-driven**, `on_bar(ctx)`: called at each bar's close with `ctx.history(field, n)` (rows up to now only), `ctx.position(symbol)`, `ctx.equity`, `ctx.cash`, `ctx.state` (a dict you keep) and the orders `order_target_weight`, `order_target_qty`, `order_qty` and `order_value`. See `strategies/example_event.py` (a channel breakout).

`from btlab.indicators import sma` and the other indicators work inside a plugin, as the vectorized example shows. A plugin receives only its bars, parameters and context: no database, configuration, IB or network.

### The rule builder

**Backtest**, Source **Rule**, **Open the rule builder**. A rule has an **Entry** and an **Exit** tree of groups (ALL of, ANY of, NOT; at most 3 levels and 32 conditions) and conditions: a left operand, an operator (greater, less, crosses above, crosses below…), a right operand, and optionally "within n bars" or "for n bars". Operands are indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Donchian, ROC, z-score, volatility, VWAP…, with a shift of k bars), numbers, spylead series, minute-derived daily features (last or first N minutes' return, close against VWAP, range position, reclaim of the previous low) and, on minute timelines, today-so-far values. Any number can be a rule parameter, so optimization can vary it. The builder validates as you go, saves rules by name, and shows the JSON read-only. **Use in a backtest** puts the rule into the form.

## 11. Optimization and its traps

**Optimize** view:

1. Pick the base: a saved backtest, or the Backtest view's current form.
2. Set a range (min, max, step, or a list of values) for each parameter to vary; strategy parameters are prefilled. **Add another path** varies other fields, for example `exits.ibguard.tp_pct` or `sizing.risk_pct`.
3. Choose the **Objective** (Sharpe by default, or CAGR, Sortino, Calmar, profit factor), **Minimum trades** (20) and the **Mode**:
   - **GRID**: every combination on the whole period;
   - **OOS**: the grid on the first 70%, then the best combination on the last 30%, side by side;
   - **WFO** (walk-forward): train 3 years, test the next year, move one year, repeat; the test years chain into one out-of-sample curve.
4. At most 2,000 combinations. When the estimate exceeds 10 minutes, btlab asks before running.
5. Results: a sortable table, a heatmap of two parameters (the others fixed at the best), in-sample against out-of-sample, the walk-forward windows. **Run as a backtest** promotes a combination to an ordinary saved backtest, which a live portfolio or ibguard can then use.

The traps:

- **Overfitting.** With hundreds of combinations, the best one is partly luck. Trust the out-of-sample and walk-forward numbers, not the in-sample best.
- **Peaks against plateaus.** In the heatmap, prefer a broad region of good values to a single bright cell.
- **Re-using the out-of-sample period.** Every time you look at the OOS result and change something, those last 30% become in-sample too.
- **Too few trades.** Keep a sensible minimum; a Sharpe from five trades means little.
- **Optimism stacks up.** SAME_CLOSE, zero slippage, in-sample spylead series (F, COMPOSITE), survivorship and ADJUSTED mode all flatter results.
- **Look-ahead.** The look-ahead check runs for the first and the best combination only.
- **Short history.** One walk-forward window needs 3 years of training plus a test period; a last test window shorter than 3 months is dropped, so a run needs at least 3 years and 3 months.

## 12. A live portfolio in test mode

A live portfolio follows one saved **daily** backtest in one market. In test mode nothing goes to IB; you get a paper-trading record on real prices.

1. Run and save a daily backtest you want to follow (§8).
2. On its result page press **Use for a live portfolio**, or open **Live**. In the market's card choose the backtest, optionally a capital (default: the backtest's initial capital), and press **Create portfolio**. Its first session is the next one after the latest stored day.
3. **A batch** (the orders for one session) appears after the market's next data update, or at once with **Regenerate**. btlab replays the backtest to the latest close, turns its positions (and orders still pending) into target weights, scales them to your portfolio's equity, rounds to whole shares (1,000-share lots in Taiwan) and lists the differences from your holdings as BUY and SELL lines. Short lines in Taiwan are skipped.
4. **Confirm before the cutoff**, shown as a countdown: 09:20 New York time for US batches (21:20 or 22:20 Taipei), 08:50 Taipei for Taiwan. Untick lines you do not want. The dialog says "virtual (test mode: nothing goes to IB)". An unconfirmed batch expires at the cutoff; a later update replaces an unconfirmed one.
5. **Virtual fills** happen when that session's data arrive, at the next update: market-on-open lines at the open; limit lines at the open if it is better than the limit, else at the limit if the day reached it, else they expire; with the backtest's slippage and costs. Dividends and splits apply to the holdings.
6. The card shows holdings, cash, equity, the batch and its history, and a curve of the portfolio against the benchmark and the replayed backtest. **Stop portfolio** ends it and keeps its history.

Timing in Taipei: the US update at 08:30 Tuesday to Saturday proposes the batch for that evening's US session; you confirm during the day; the next morning's update fills it and proposes the next. The Taiwan update at 18:00 proposes the batch for the next morning; you confirm by 08:50; that evening's update fills it.

## 13. Trade mode

Trade mode sends the batches you confirm to IB as real orders. Use the **paper** account first, for weeks.

### Turning it on

1. IB Gateway logged in to **paper**, "Read-Only API" unchecked, socket port equal to `[ib] port` (§15).
2. ibguard running (on its paper database while you test), so sells are coordinated.
3. In `config.toml`, add your own limits (there are no defaults) and switch the mode:

   ```toml
   mode = "trade"

   [limits]
   us_max_orders_per_day = 5      # lines sent per US session
   tw_max_orders_per_day = 5      # lines sent per Taiwan session
   max_order_pct_of_equity = 20   # one line's value, as % of the portfolio's equity
   us_max_order_value = 10000     # USD per line
   tw_max_order_value = 300000    # TWD per line
   ```

   These numbers are examples: choose yours. btlab refuses to start in trade mode while one is missing or not above 0.
4. Restart btlab. Home shows **TRADE·paper**, or **TRADE·live** with a red banner when Gateway is logged in to a live account. A test-mode portfolio does not carry over: create the trade-mode portfolio in the Live view.

### What changes

- Batches start from IB's positions and the ledger's cash. Contracts come from IB; Taiwan quantities round to IB's size increments; limit prices round to IB's tick grid (buys down, sells up). Lines above a limit are BLOCKED_LIMIT; past the count limit the largest lines stay.
- On confirmation btlab checks again (connected, right account, before the cutoff, limits, and IB's positions unchanged, else BATCH_STALE: regenerate) and sends: sells and covers first, then buys. US market-on-open orders go as MKT with time-in-force OPG; limit orders as DAY; never outside regular hours; each with an `orderRef` starting `btlab:` that you can find in TWS.
- **Cancel working orders** cancels btlab's own working orders of the batch. Orders that are not btlab's, or belong to another client id, are never touched, and btlab never cancels everything.
- Order states, fills and commissions come back from IB and update the ledger. After a restart btlab matches IB's orders to its lines again by their exact `orderRef`. 15 minutes after each session's end it checks every line that has not ended; day orders IB cancelled at the close become EXPIRED.
- If IB answers that the API is read-only, the batch stops and you get the READ_ONLY_API event and email.

### Coordination with ibguard

For every sell (or short cover) of a position ibguard protects, btlab: reads ibguard's state; skips the line when ibguard is exiting the position or cannot be reached (SKIPPED_IBGUARD_EXITING, SKIPPED_IBGUARD_UNREACHABLE: a btlab sell racing an ibguard exit could open a reverse position); otherwise excludes the position in ibguard, checks that ibguard shows it excluded, then sends. When the order ends, btlab restores ibguard's protection if a position is left (and emails PROTECTION_RESTORED) or marks it closed. A position you excluded yourself stays excluded. A failed restore is retried every minute, with one event and one email.

### Writing ibguard's thresholds

A backtest with the **ibguard rule** in its exits holds a take-profit and stop-loss pair. On its result page press **Write to ibguard…** (or use the **ibguard thresholds** panel in the Live view). **Preview** shows ibguard's account and whether it is paused, the current and new STK thresholds, and every stock position, US and Taiwan, that would exit at once under the new pair. **Write to ibguard** asks for confirmation and writes the STK pair only; if positions changed since the preview, it answers PREVIEW_CHANGED and you preview again. ibguard keeps one pair for both markets. This works in test and trade mode alike, because it changes ibguard, not btlab's orders.

## 14. iPhone and iPad

btlab listens only on the PC (127.0.0.1). Tailscale Serve publishes it to your own devices inside your tailnet, over HTTPS.

1. Install Tailscale on the PC, the iPhone and the iPad, signed in to the same tailnet. In the Tailscale admin console, turn on MagicDNS and HTTPS certificates (once).
2. On the PC:

   ```powershell
   tailscale serve --bg --https=10000 http://127.0.0.1:8091
   ```

   It stays on across restarts (`tailscale serve status` shows it; `tailscale serve --https=10000 off` removes it). spylead uses port 8443, so the ports do not collide.
3. On the iPhone or iPad, open `https://<pc-name>.<tailnet>.ts.net:10000` in Safari and log in.
4. Share, **Add to Home Screen**. From now on open btlab from that icon: a home-screen app keeps its saved results, while Safari deletes a website's data after 7 days without a visit.

**Offline**: without a connection to the PC, the app says "Offline — saved results only" and shows the 20 results you opened most recently plus every result marked **Keep offline**; running, updating and confirming are disabled. Logging out deletes the saved results on that device.

**Never use Tailscale Funnel or open a public port** (router port forwarding, a firewall rule, or `[web]` on another address). That would put btlab on the internet, where only the web token would stand between strangers and your data, your IB positions and, in trade mode, your orders. Tailscale Serve keeps it among your own signed-in devices.

## 15. IB Gateway

- **API settings** (Configure, Settings, API, Settings): enable socket clients; socket port 4002 for paper or 4001 for live, equal to `[ib] port`; allow connections from localhost only (trusted IP 127.0.0.1).
- **Client ids**: btlab uses 31, ibguard 17. They must differ; btlab refuses 17.
- **Minute bars** need IB's market-data permissions for the market; any login works for history, including paper and "Read-Only API".
- **Trade mode** needs "Read-Only API" unchecked. In test mode btlab connects read-only and never constructs its order module.
- **Taiwan**: IB does not offer Taiwan market access to residents of Taiwan. Taiwan minute bars and Taiwan orders through IB may therefore be refused (NO_PERMISSION, or rejected orders). Taiwan backtests and Taiwan test-mode portfolios work regardless, on FinMind's daily data.
- **While Claude Code builds or changes btlab on this PC**, keep IB Gateway closed, logged in to paper, or set to "Read-Only API".

btlab connects to IB when a job first needs it. In trade mode it stays connected while a btlab order works or ibguard's protection is lifted, and reconnects after 10 s, waiting up to 60 s between tries.

## 16. Email

Optional. In `config.toml`:

```toml
[email]
smtp_host = "smtp.gmail.com"
smtp_port = 587              # 465 uses TLS from the start; other ports use STARTTLS
username = "you@gmail.com"
password = "app-password"    # for Gmail an app password, not your account password
sender = "you@gmail.com"
recipient = "you@gmail.com"
language = "zh-TW"           # or "en"
```

Emails, with subjects starting `[btlab]` (`[btlab][TRADE]` in trade mode): BATCH_READY (a batch waits, with its cutoff), BATCH_DONE (a sent batch has ended, with fills), PROTECTION_RESTORED and IBGUARD_RESTORE_FAILED (ibguard coordination), UPDATE_FAILED (a scheduled update failed 3 times), READ_ONLY_API, and INTERNAL_ERROR (at most once per run). A failed send is logged and never stops anything.

## 17. Every day: autostart, stopping, the drive and backups

### Start at sign-in

```powershell
cd E:\00_CODING\btlab
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
```

This registers a task named **btlab** that runs at your sign-in. It runs a small launcher, `%LOCALAPPDATA%\btlab\launch.ps1` on the C: drive, which waits up to 10 minutes for `E:\00_CODING\btlab` (the external drive may mount late), then starts btlab in the background without a window (`pythonw.exe`). It notes what happened in `%LOCALAPPDATA%\btlab\launch.log`: started, the folder never appeared, or btlab stopped at once with its exit code (1: see `logs\btlab.log`; 2: `config.toml` or `taxes.toml`; 3: btlab already runs). If Windows refuses the task, the script puts a shortcut to the launcher in your Startup folder instead and says so.

- Start it now without signing out: `Start-ScheduledTask -TaskName btlab`.
- Remove it: the same command with `-Remove` at the end. A running btlab keeps running.

### Stopping btlab

- Started with `run.bat`: Ctrl+C in its window. btlab stops accepting requests, interrupts running jobs (data stored so far stay), disconnects IB (orders at IB stay) and closes its databases.
- Started by the autostart: Task Manager, Details, `pythonw.exe` (the one whose command line names btlab), End task. That skips the orderly steps but is safe: the databases survive, orders at IB stay, and an interrupted job simply runs again next time. Prefer a moment when Home shows no job running.

### Keeping the PC awake

While a job runs, or a btlab order works at IB, btlab asks Windows not to sleep (`powercfg /requests` lists it). Otherwise the PC sleeps as you set it; a sleeping PC runs a missed update when it wakes.

### A fixed drive letter

btlab and the launcher expect `E:\00_CODING\btlab`. In Disk Management, right-click the external drive's volume, **Change Drive Letter and Paths**, and assign `E:`; Windows then keeps that letter for the drive. Without the drive btlab cannot start. On exFAT, Git needs the `safe.directory` entry of §4.

### Backups

Everything btlab keeps lives in `data\` (`btlab.db`: lists, bars, results, live portfolios; `minute.db`: minute bars). Stop btlab, then copy the whole `data\` folder somewhere else; a copy taken while btlab runs may be inconsistent. Keep a private copy of `config.toml` too: it holds your keys, so never commit or share it. `logs\` can be deleted any time btlab is stopped.

### Updating btlab

```powershell
git pull
.venv/Scripts/python -m pip install -r pylock.toml   # only when pylock.toml changed
```

Then restart btlab.

## 18. Bench and checks on the PC

`.venv/Scripts/python -m btlab.bench` times three cases on synthetic data with the real process pool (run it while btlab is idle):

| Case | Data | Target |
|---|---|---|
| 1 | One daily run: 50 symbols × 30 years, MA_CROSS with the ibguard exit, REALISTIC, with dividends and splits | ≤ 5 s |
| 2 | 2,000 combinations of case 1 | ≤ 10 min |
| 3 | One minute run: 10 symbols × 3 years of 1-minute bars, a rule with daily operands and close-out | ≤ 60 s |

The checks to do by hand (SPEC.md §24.3):

1. Install from `pylock.toml` and run the full test suite on Windows.
2. Fill `config.toml`, start btlab, update both markets. Update again: "already up to date" (UP_TO_DATE). Compare a few closes with Tiingo's and FinMind's websites, and one split or dividend in raw against adjusted candles.
3. Run the bench and compare with the targets.
4. The UI checklists in `plans/prompt-17.md`, `prompt-18.md` and `prompt-19.md`: Edge installed as an app, an iPhone at 375 px and an iPad in both orientations through Tailscale Serve, added to the home screen. Offline: disconnect, then open saved results from the home-screen icon.
5. IB, paper or read-only: backfill a few days of one US symbol and check its coverage; try one Taiwan symbol (OK or NO_PERMISSION).
6. Test mode: create a US live portfolio from a daily backtest; after the next update a batch appears; confirm it; after the following update, check the virtual fills and the paper curve.
7. Trade mode on paper: set your limits, `mode = "trade"`, IB Gateway on paper with "Read-Only API" unchecked, ibguard on its paper database. Confirm a small batch and find the `btlab:` orders in TWS. Sell a position ibguard protects, then cancel the working order before the open: ibguard excludes the position, then restores it, and the email arrives. `plans/prompt-23.md` has the detailed checklist.
8. Install the autostart, sign out and in, and check that btlab comes up after the drive mounts (`plans/prompt-24.md` has the checklist).

## 19. Troubleshooting

| You see | Meaning and fix |
|---|---|
| The window closes or `launch.log` says exit code 2 | `config.toml` or `taxes.toml` has a mistake; `run.bat` shows which key. |
| Exit code 3, "another btlab process is running" | One btlab already runs (perhaps the autostart). Only one may use `data\`. |
| MISSING_KEY | That source's key is empty in `config.toml`. |
| HTTP_401 or HTTP_403 from a source | The key is wrong, or FinMind's email is not verified yet. |
| "waiting for Tiingo's hourly limit" | Normal on a first update; it continues by itself. |
| RATE_LIMITED | The service refused for now; the next update or a retry continues. |
| MAINTENANCE | FinMind's Sunday window; the update runs again after 07:05 Taipei. |
| SYMBOL_UNKNOWN | Tiingo does not know the ticker; check its spelling (`BRK.B`). |
| NO_PERMISSION (minute bars) | IB has no market-data permission for that market; see §15 for Taiwan. |
| IB_UNREACHABLE, NOT_CONNECTED | IB Gateway is not running, not logged in, or on another port than `[ib] port`. |
| NO_MINUTE_DATA | A minute backtest's symbols have no minute bars: mark them and backfill. |
| JOB_RUNNING | That lane is busy; wait or cancel the running job on Home. |
| LIVE_DATA_INCOMPLETE | A portfolio symbol lacks the latest session's bar; the next update tries again. |
| BATCH_EXPIRED | The cutoff passed; the next update proposes a new batch. |
| BATCH_STALE | IB's positions changed since the batch was made: **Regenerate**. |
| READ_ONLY_API | IB Gateway is set to "Read-Only API"; uncheck it for trade mode. |
| SKIPPED_IBGUARD_… | ibguard was exiting the position, unreachable, or did not confirm the exclusion; the sell was not sent. |
| HOLDINGS_MISMATCH | IB's quantities differ from what the ledger expects (a manual trade, a partial fill); btlab trusts IB's quantities. |
| PREVIEW_CHANGED | ibguard's positions changed between preview and write: preview again. |
| LOOKAHEAD_SUSPECT | Your plugin uses future data somewhere; fix it before trusting the result. |
| INTERNAL_ERROR | An unexpected error; `logs\btlab.log` has the details. btlab keeps running. |

To reload a changed plugin without a restart, run this in the browser's developer console while logged in: `fetch("/api/strategies/reload", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})`.

## 20. Limitations, in plain words

These are your own decisions, all intentional (SPEC.md §25):

1. **Survivors only.** Lists hold only symbols that exist today, so backtests never meet the companies that failed; results look better than reality.
2. **Inside a bar** btlab guesses the order of high and low from the daily bar wherever minute bars are missing, and minute bars cover only chosen symbols and about 3 years.
3. **Fills** happen at the open or close with a fixed slippage: no partial fills, no auction imbalances, no market impact. Test-mode limit orders fill at the open or at the limit.
4. **Corporate actions** are approximated in places: capital-reduction refunds count as share value; fractional shares are paid at the reference price; a Taiwan stock-and-cash dividend without a policy row becomes a share ratio; dividends arrive on the ex-date, not the pay date.
5. **Taiwan's limit lock** is detected by a rule of thumb.
6. **Shorting** is simplified: no uptick rule, no recalls, a constant borrow fee, no margin calls, and Taiwan shorts are covered when a short-sale suspension starts.
7. **Cash** earns no interest; there is no borrowing on margin; each backtest has one currency.
8. **ADJUSTED mode** reinvests dividends untaxed and applies lots and minimum commissions to adjusted prices.
9. **Costs** leave out IB's Taiwan clearing fees and tier changes, US regulatory fees and the rounding of Taiwan's tax. Taiwan defaults assume IB; a Taiwan broker's commission can be entered per backtest.
10. **IB and Taiwan**: IB does not offer Taiwan market access to residents of Taiwan, so Taiwan orders and minute bars through IB may be unavailable; btlab notices at run time, and Taiwan live portfolios still work in test mode.
11. **Live portfolios** run daily strategies only and trade at the next open. A strategy's own stops are not executed live; ibguard protects positions in real time. Live holdings drift from the backtest through rounding, skipped lines and real fills, and Taiwan live orders use IB's lot size.
12. **spylead's F and COMPOSITE** are fitted over their whole history, so backtests that use them are optimistic.
13. **The PDT flag** is informational and stops at 2026-06-04.
14. **Optimization can overfit**; the out-of-sample check and walk-forward only measure it.
15. **Data licences**: Tiingo for internal use, FinMind for non-commercial use with attribution, FRED under its terms.
16. **The PC** must be on, with btlab running, for the iPhone and iPad to use it; offline they show saved results only. Without the external drive btlab cannot start.
17. **US holidays** after the last stored SPY bar are unknown, so scheduling treats every weekday as a trading day (a holiday's update finds nothing new).
18. **Plugins** run with your privileges, because they are your code.

Everything deferred is listed in `TODO.md`.
