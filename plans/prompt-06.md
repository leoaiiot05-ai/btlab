# Prompt 6: Engine core

Covers SPEC.md §3, §4.1, §6.5, §9.1, §9.4, §10.1 to §10.7, §10.10, §10.11 (without metrics) and the timeline of §11.2. Acceptance: AC-7, AC-8, AC-9, and the "orders wait for the next tradable bar" part of AC-5.

## 1. Steps

1. **`btlab/costs.py`** (pure), the costs of §10.6:
   - `CostModel(market, commission, slippage_bps, dividend_withholding_pct, categories, taxes=None, borrow_fee_pct=(), short_fee_pct=0.0)`: `commission` is markets.py's `PerShareCommission` (US) or `RateCommission` (TW); `categories` and `borrow_fee_pct` hold one entry per symbol (the per-symbol override already resolved; empty in TW); `taxes` is the loaded `TaxTable` (None in US).
   - Methods: `commission(quantity, value)`: US `min(max(per_share × q, min), max_pct% × value)`, TW `max(rate_pct% × value, min)`, 0 without a quantity; `fill_price(price, buy)`: ± `slippage_bps`; `tax_rate(symbol, day, day_trade)` through taxes.py (0 in US); `borrow_fee(symbol, short_value, days)`: value × pct/100 × days/365, 0 without a rate; `short_sale_fee(value)`; `net_dividend(amount)`: less withholding.
2. **`btlab/sizing.py`** (pure), §9.4:
   - `Sizing(mode="FIXED_FRACTION", fraction=None, amount=None, shares=None, risk_pct=1.0, atr_n=20, atr_mult=2.0)`; `fraction` None means 1/N.
   - `entry_quantity(sizing, equity, price, atr, symbols)`: FIXED_FRACTION `fraction × equity / price`, FIXED_AMOUNT `amount / price`, FIXED_SHARES `shares`, VOL_RISK `risk_pct/100 × equity / (atr_mult × ATR)`; 0 when it is not a finite positive number.
   - `to_lot(quantity, lot)`: rounded toward zero to the lot (§10.4); `cap(quantity, equity, price, max_position_pct)`.
3. **`btlab/engine.py`** (pure), one loop for daily and minute timelines:
   - **`Timeline(symbols, time, day, session, open, high, low, close, adj_open, adj_high, adj_low, adj_close, tradable, lock, cash, ratio, start)`**: `time` holds day numbers (daily) or UTC epoch seconds (minute); `day` the day of each bar's session; `session` a non-decreasing session index; `cash` and `ratio` the corporate actions on a session's first bar; `start` the first bar of trading and equity (§10.1 warm-up). `daily_timeline(aligned, start)` builds it from prices.py's `Aligned`; prompt 11 builds the minute one.
   - **Strategy outputs (§9.1):** `Targets(kind, values, rebalance=None)` for DIRECTION and WEIGHTS (bars × symbols, NaN = no instruction), or an `OrderStrategy` with `on_bar(book)` (ORDERS). `Book` offers `t`, `time`, `positions`, `equity`, `cash`, `price(j)` and `order_target_qty`, `order_qty`, `order_target_weight`, `order_value`; the last order for a symbol in a bar wins. Plugins' `ctx` (prompt 10) and DCA (prompt 9) build on it.
   - **`Settings(market, initial_capital, costs, sizing, price_mode, fill_timing, allow_short, board_lots_only, max_position_pct, suspensions)`**; `suspensions` holds each symbol's TW short-sale suspensions.
   - **`simulate(timeline, strategy, settings, exits=None) -> EngineResult`**. Each bar, in the order of §10.3:
     1. On a session's first bar: corporate actions (REALISTIC only, §10.7), due TW forced covers, borrow fees on shorts (US), and a fresh day-trade record.
     2. Open fills: forced covers always at the open; pending orders under NEXT_OPEN.
     3. **The hook for intrabar exits** (`exits`, prompt 7). None in this prompt.
     4. Close: value positions and record equity; max-hold orders (prompt 7, through the same hook); evaluate the strategy into orders; under SAME_CLOSE fill them, and pending ones, at this close and record equity again.
     5. End: open positions stay, valued at the last close; their trades end with exit reason OPEN at that close, without exit costs.
   - **Targets to orders.** An order holds a symbol's target quantity, rounded toward zero to the lot (1, or 1,000 with board lots), and replaces any pending order of the symbol.
     - DIRECTION: a direction that differs from the last one instructed enters, exits or reverses; the entry is sized by §9.4 at the signal bar's close (equity and price raw in REALISTIC; the ATR converted to the raw scale with raw close ÷ adjusted close), capped at `max_position_pct`; then the quantity is held until the direction changes (no drift rebalancing).
     - WEIGHTS: on a rebalance bar every non-NaN target executes; on other bars only targets that moved more than 1e-9 from the last executed one; quantity `w × equity / price`.
     - Negative targets without `allow_short` count as 0 and set SHORTS_IGNORED.
     - An order smaller than one lot is skipped; an order to 0 always closes the whole position (see the readings).
   - **Fills (§10.5).** At each fill moment the change `target − current` splits into a reducing leg (toward 0) and an increasing leg. Reducing legs of every symbol fill first; then the increasing legs, scaled together when they need more than the buying power `BP = E − long − m × |short|` (m = 0.5 US, 0.9 TW), each rounded down to the lot. A leg does not fill on an untradable bar, nor against a limit lock (locked up blocks buys and covers, locked down sells and short sales), nor as a TW short sale inside a suspension; its order stays pending and is retried at the next open (NEXT_OPEN) or close (SAME_CLOSE). A scaled order is done.
     - Fill prices: the open of the fill bar (NEXT_OPEN) or the close (SAME_CLOSE), ± slippage. Cash: a buy pays `q × p + costs`, a sell receives `q × p − costs`; short-sale proceeds stay in cash.
     - Costs: one commission per symbol and fill moment: the reducing leg pays the commission on its own quantity, the increasing leg the rest of the commission on both together. Sells pay the transaction tax of the fill day and category; the part of a sell that closes shares bought in the same session pays the day-trade rate; a cover of a short sold in the same session credits back the difference between the normal and the day-trade rate on the covered quantity, at the session's short-sale price. TW short sales pay the short-sale fee.
   - **Shorting and actions (§10.7).** Borrow fees: short value at the previous close × rate × calendar days since the previous session / 365. TW forced cover: a short open at the close before a suspension's start is covered at the open of the first tradable bar on or after it (FORCED_COVER), ahead of other orders; no short sale fills through the suspension's end. Share events: `trunc(q × r)` shares, the remainder settled in cash at the previous raw close ÷ r, pending targets × r. Cash dividends: longs receive `q × D` less withholding, shorts pay `|q| × D`. US applies the share ratio first and pays D on the new quantity (Tiingo quotes D per post-split share); TW pays D on the old quantity first (`權息`: D per old share, then the stock dividend).
   - **Trades (§10.5).** A trade opens when a position leaves 0 and closes when it returns to 0; a reversal closes one and opens another. Average cost: a partial reduction realizes P&L at the average entry price. P&L is net of commissions, taxes, short fees and borrow fees, plus dividends received or minus dividends paid while open; the cash in lieu of a share event is realized like a reduction. `quantity` is the total entered, `entry_price` and `exit_price` the averages, `return_pct` P&L ÷ the entered value × 100.
   - **`EngineResult`**: from `start`: `time`, `equity`, `cash`, `long_value`, `short_value`, `gross` and `net` exposure as fractions of equity, `positions` (count), `quantity` (bars × symbols); `trades`; `fills` (time, symbol, side, quantity, price, costs, reason: markers and turnover for prompts 8 and 16); `flags` (SHORTS_IGNORED, DATA_ENDED when a symbol's data end while a position is held); the final `holdings` and `pending` targets (live replay, prompt 22).
   - The engine draws no random numbers and iterates symbols in index order, so equal inputs give bit-identical results (§10.10).
4. **Tests** (`tests/test_costs.py`, `tests/test_sizing.py`, `tests/test_engine.py`), hand-made bars of 2 or 3 symbols over a few dozen bars:
   - AC-7: the identity `equity = cash + long − short` on every bar; NEXT_OPEN and SAME_CLOSE fills with slippage; reducing before increasing orders and pro-rata scaling when buying power is short; lots and skipped sub-lot orders with board lots; US commissions (per share, minimum, the 1% cap below the minimum) and the TW minimum; TW taxes by category and date; the day-trade rate for same-session round trips, long and short, on a timeline with several bars per session; trade P&L net of everything; partial reductions at average cost.
   - AC-8: shorts ignored without `allow_short` (SHORTS_IGNORED); buying power with m = 0.5 and 0.9; the borrow fee over a weekend; US short dividends; the TW short-sale fee; forced cover at the first tradable bar on or after a suspension's start, and a short sale waiting through its end.
   - AC-9: a share event with cash in lieu for a long and a short; a stock dividend; a cash dividend with withholding; pending targets scaled; value continuity on an ex-date whose open equals the reference price (US split with dividend, TW `權息`); ADJUSTED mode applies no action, and its buy-and-hold return without costs equals the adjusted-price return.
   - AC-5's engine part: an order waits through untradable bars and a lock in its direction, and a lock in the other direction does not block it.
   - Sizing: every mode, the cap, the held quantity; WEIGHTS with and without a rebalance mask; ORDERS through `Book` with the last order winning; DATA_ENDED; determinism (two runs give identical arrays).
5. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **An order to 0 always closes the whole position**, even below one board lot: otherwise an odd-lot position (after a stock dividend or cash in lieu) could never be closed.
- **A buy-power shortfall scales the order, which is then done**: the unfilled part is not retried (only untradable bars and locks keep an order pending, §10.5).
- **One commission per symbol and fill moment**, also for a reversal ("commission per fill"), shared between the closing and the opening trade as above.
- **Same-day share ratio and cash dividend**: the order of application follows each source's definition of D (above).
- **Borrow fees** use the short value at the previous bar's close, before the session's actions change the quantity.

## 2. Extension points

- **Exit overlays** plug into `exits` (step 3 and the max-hold orders of step 4), prompt 7.
- **Minute timelines** are another `Timeline`; the loop already keys sessions, day trades, borrow fees and actions on `session` and `day` (prompt 11 adds close-out and the PDT count).
- **A strategy output** is `Targets` or an `OrderStrategy`; plugins and DCA use `Book`.
- **Costs** are one `CostModel`; a new fee is one method and one call in the fill.

## 3. Refactor seams

- Costs and sizing are separate pure modules; the engine only calls them.
- The loop's steps are separate methods (session start, open fills, exits, close), so a step changes alone.
- Orders hold target quantities only; fills split them into legs, so reversals, partial fills and blocked legs share one path.
- Trade bookkeeping is one small class per open trade.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-7, AC-8 and AC-9 hold, and so does AC-5's engine part.** `.venv/bin/python -m pytest -q`: 614 passed (also with `-X dev -W error`).
- Built as planned, with these names and one addition:
  - `CostModel.commission_rule` holds the markets.py rule, because `commission(quantity, value)` is the method.
  - **Exit reason CASH_IN_LIEU** (`engine.EXIT_REASONS`): a position that a share event leaves below one share is settled in cash entirely, and its trade ends with this reason. §10.8's list has no reason for it; prompt 17's locale test collects it from the constant.
  - Borrow fees are charged just before the session's corporate actions rather than after; the amount is the same (the short value at the previous close), and a short closed by cash in lieu still carries its last fee.
- Timing at bench size (50 symbols × 7,800 bars, MA cross with 4,215 trades, synthetic splits and dividends, no exits): 0.55 s on this VM.
- **For prompt 7:** `simulate(..., exits=...)` calls `exits.intrabar(simulation, t)` after the open fills and `exits.at_close(simulation, t)` before the strategy; `Simulation.order(j, target, reason)` places an order, `Simulation.fill(t, prices, orders)` fills at a moment, and `open_trades`, `q`, `price`, `instructed` and `executed` are the state an overlay reads.
