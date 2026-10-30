# Prompt 4: Price series and calendars

Covers SPEC.md §3, §4, §6 and §7 for the pure price layer. Acceptance: AC-4 and AC-5 (the part of AC-5 about orders waiting for a tradable bar is the engine's, prompt 6).

## 1. Steps

1. **`btlab/prices.py`** (pure: numpy, the records and calendar.py; no I/O, clock or logging).
   - **`ActionIssue(kind, day, message)`**, kind BAD_ACTION or DIVIDEND_SPLIT_UNKNOWN. Returned as values; the caller records events (prompt 14).
   - **`normalize_us_actions(actions) -> (actions, issues)`** for Tiingo's actions (prompt 3): a cash amount or ratio that is not finite, a ratio ≤ 0 or a cash amount ≤ 0 is skipped with BAD_ACTION; a ratio within 1e-6 of 1 is dropped.
   - **`normalize_tw_actions(results, policies, splits, par_changes, reductions) -> (actions, issues)`**, the rules of §6.2:
     - D on day t sums `cash` over the policy rows whose cash ex-date is t; "a policy row matches" means at least one such row.
     - `息`: CASH_DIV(D), or CASH_DIV(P − A) without a matching row. `權`: SHARE_RATIO(P / A). `權息` with D: CASH_DIV(D) and SHARE_RATIO((P − D) / A). `權息` without: SHARE_RATIO(P / A) and DIVIDEND_SPLIT_UNKNOWN. Details DIV_RESULT (cash) and STOCK_DIV (shares), with `ref_before = P` and `ref_after = A`.
     - Splits: SHARE_RATIO(before / after), detail SPLIT above 1 and REVERSE_SPLIT below. Par-value changes: SHARE_RATIO(before_close / after_ref_close), PAR_CHANGE. Capital reductions: SHARE_RATIO(closing / reference), CAPITAL_REDUCTION.
     - A dividend result whose P or A is not a positive finite number, an unknown dividend kind, a second dividend result on one day, and every amount the US rules reject, are BAD_ACTION. A ratio within 1e-6 of 1 is dropped. Exact duplicate rows count once.
     - **One action per kind and day**, as the store's primary key holds: ratios of one day multiply and cash amounts add (§6.3: "several events on one day multiply"). The merged detail is the first event's in the order dividend result, split, par value, capital reduction. On a day with a dividend result, both of the day's actions carry `ref_before = P` and `ref_after = A ÷ Π r` over the day's other share events: the reference price after all of the day's events, so the factor below stays `ref_after / ref_before`.
   - **`day_events(market, bars, actions) -> (DayEvents, issues)`**: per event day, ascending: `cash` (sum), `ratio` (product) and the factor g of §6.3:
     - US: `g = (C_prev / r − D) / C_prev`, with C_prev the last finite raw close before the day; without one, `g = 1 / r`.
     - TW: `g = ref_after / ref_before` on a dividend-result day, else `g = 1 / r`.
     - The (0, 2] check: see the readings below.
   - **`adjusted(bars, events) -> DailyBars`**: `adj_price(τ) = raw(τ) × Π g_t` and `adj_volume(τ) = raw_volume(τ) × Π r_t` over the event days t after τ, by suffix products. Events after the last bar still count: adjusted prices are relative to the latest stored price. Callers pass every bar and action through the latest stored day.
   - **`align(market, days, histories) -> (Aligned, issues)`** (§6.4), `histories` mapping each symbol, in run order, to its `(DailyBars, actions)`:
     - `Aligned`: `days`, `symbols`, raw `open/high/low/close/volume`, `adj_open/adj_high/adj_low/adj_close/adj_volume`, `multiplier` (adjusted ÷ raw scale of each day, which minute features use, §8.2), `tradable`, `lock` (+1 locked up, −1 locked down, 0), and the engine's per-day `cash` and `ratio` (0 and 1 without an event). Arrays are float64 `(T, N)`, `tradable` bool and `lock` int8.
     - NaN before a symbol's first stored bar and after its last. Inside, a missing or no-trade day carries the last traded raw close into open, high, low and close, with volume 0 (§6.4); its adjusted prices carry the last traded adjusted close, so the adjusted series stays flat across the gap.
     - Tradable (§6.5): a stored bar of that day, not a no-trade day, volume above 0, and finite prices.
     - `lock` from calendar.py with `ref` = the last traded raw close before the day × the day's g (§6.5).
     - An action on a day missing from the calendar applies on the next calendar day; one after the last calendar day only enters the multipliers.
2. **`btlab/calendar.py`** (pure: numpy, zoneinfo, markets.py and records.py):
   - `trading_days(stored, through)`: the stored days (US: the days of SPY's bars; TW: FinMind's calendar), then the weekdays after the last stored day through `through` (§4.3).
   - `next_session(stored, day)`: the first trading day after `day`, by the same rule.
   - `weekday(days)`; `session(market, day) -> (start, end)` in UTC epoch seconds through zoneinfo (§4.2); `session_end(market, day, last_minute_t=None)`: the end of the last stored minute bar, or the regular close.
   - `period_starts(days, frequency)`: True on the first bar and on every bar whose calendar month (`monthly`) or ISO week (`weekly`) differs from the previous bar's (§9.3).
   - `price_limit_pct(market, days)` from `Market.price_limits` (NaN in US), and `limit_lock(market, days, open, high, low, close, ref)` (§6.5): locked when `open = high = low = close` and `|close / ref − 1| ≥ 0.9 × limit`, up when the close is above `ref` and down when below; NaN or a missing `ref` never locks.
3. **records.py**: `DayEvents(day, cash, ratio, factor)`, `eq=False`, next to the other array records.
4. **Tests** (`tests/test_prices.py`, `tests/test_calendar.py`), small hand-made data:
   - AC-4: A1 to A6 of §6.3 through normalization, `day_events` and `adjusted`, including A2's doubled volume and A5's ratio 1.1; each TW dividend case of §6.2 (`息` with and without a policy row, `權`, `權息` with and without D, a ratio within 1e-6 of 1); split, reverse split, par-value and capital-reduction ratios; same-day events merged into one action per kind, with the factor of all of them; bad actions (non-finite, zero, negative, unknown kind, bad P or A) as BAD_ACTION values; a factor outside (0, 2]; adjusted volumes across several events.
   - AC-5: TW days from the stored calendar and US days from SPY's days plus weekdays; `next_session` across a weekend and past the stored days; sessions in UTC across both US daylight-saving changes and in Taipei; `session_end` with and without minute bars; period starts by month and by ISO week across a year end; alignment over a missing day, a no-trade day, a symbol listed mid-period and one whose data end early, with the carried closes, volume 0, NaN outside the range, the tradable flags and the per-day cash and ratio; an action on a non-calendar day; the limit lock before and after 2015-06-01, up and down, just inside and just outside the 0.9 margin, and only on flat bars; US never locks.
   - Purity: prices.py and calendar.py import no I/O, logging, aiohttp or ib_async module (a source scan).
5. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent or self-contradictory

- **The (0, 2] factor check of §6.3 applies to the factor net of the day's share ratio, `g × r`.** Read literally, it would ignore every reverse split of more than 1-for-2, yet A6 (a 1-for-7 reverse split, g = 7) is a required example with its adjustment applied. Net of the share ratio, the check catches what it is for: a dividend at or above the price (US `C_prev / r − D ≤ 0`) or a TW reference price beyond twice the previous one. When it fails, only the dividend part of the factor is ignored: g becomes `1 / r` and BAD_ACTION is returned. The cash dividend itself stays, since its amount passed §6.2's checks.
- **A cash amount ≤ 0 is BAD_ACTION.** §6.2 names non-finite amounts and ratios ≤ 0; a negative dividend (for example `息` with A above P) would charge longs, so it is skipped too.
- **Days after FinMind's last listed trading day count as weekdays**, as §4.3 says for US, so the next TW session is always known (FinMind's calendar may list only past days).
- **The run's first bar is a period start** for `period_starts`; whether a strategy acts on it is prompt 9's.
- Back-adjusting from the latest stored data (§6.3, intentional) means absolute price levels in adjusted terms depend on later actions. Ratios, crosses and indicators of returns are unaffected; a rule comparing a price with a fixed value is not. Noted here for the audit of prompt 25.

## 2. Extension points

- A new action source adds a normalizer that returns actions and issues; `day_events`, `adjusted` and `align` take normalized actions from any source.
- A market with another limit rule is a `price_limits` entry in markets.py; `limit_lock` reads it.
- Another period (for example quarterly) is one more key function in `period_starts`.

## 3. Refactor seams

- Normalization, factors, adjustment and alignment are separate functions over records, so the update job (prompt 14) can normalize and report issues once, and the backtest loader (prompt 8) only aligns.
- `align` takes plain records; it never reads the store.
- Day numbers and their conversions live in records.py; calendar.py owns weekdays, sessions and periods. taxes.py can switch to `to_day`.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-4 holds, and AC-5 except "orders wait for the next tradable bar"**, which the engine owns (prompt 6); `Aligned.tradable` and `Aligned.lock` are its inputs. `.venv/bin/python -m pytest -q`: 414 passed (also with `-X dev -W error`).
- `tests/test_purity.py` checks the imports and clock or file calls of the pure modules so far (markets, records, calendar, prices); later prompts add theirs to `PURE`.
- **Read before prompt 6:** `Aligned.cash` and `Aligned.ratio` are the corporate actions at each day's open; `Aligned.lock` is already masked to tradable bars; a carried day has volume 0 and is untradable; `multiplier` is NaN outside a symbol's data.
- **For the owner (spec ambiguity):** §6.3's "A factor outside (0, 2] is ignored" contradicts example A6 when read literally; the code checks the factor net of the share ratio (see "Readings" above). If the owner meant something else, only `MAX_NET_FACTOR` and `_net_factor` in `prices.py` change.
