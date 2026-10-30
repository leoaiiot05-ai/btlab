# Prompt 7: Exits and intrabar paths

Covers SPEC.md §5.4 (minute coverage), §10.7 (levels under share events) and §10.8. Acceptance: AC-10.

## 1. Steps

1. **`btlab/exits.py`** (pure):
   - `ExitRules(stop_loss_pct, take_profit_pct, trailing_pct, atr_n, atr_mult, max_hold_bars, ibguard_tp_pct, ibguard_sl_pct)`, each None when its overlay is off; percentages as in the run configuration (§10.1).
   - **The ibguard rule** as a pure function `ibguard_step(side, ref, prev, point, tp, sl) -> (ref, prev, result)` with `tp`, `sl` as fractions: skip a point that is not finite or ≤ 0; ratchet first (long: `p < prev` and `prev > ref` gives `ref = prev`; short mirrored); then TAKE_PROFIT or STOP_LOSS, boundaries inclusive; then `prev = p`. `prev` None means unset (the vectors of §10.8); a backtest starts with `ref = prev = entry`.
   - **Paths:** `Path(points, gaps)`, where `gaps[i]` says the price jumped to point i (the bar's open, or a minute open that differs from the previous close); between other points the price moves continuously.
     - `daily_path(o, h, l, c)`: O → L → H → C when C ≥ O, else O → H → L → C.
     - `minute_path(opens, highs, lows, closes)`: each 1-minute bar contributes O, then L and H in the order of the same rule, then C. It serves the minute path of a day and the path of a minute-timeline bar (prompt 11).
   - **Triggering:** `walk(path, side, entry, atr_distance, rules, watch)` walks the points in order. At each point it updates the trailing extreme (peak for longs, trough for shorts) and the ibguard ratchet, then checks the levels:
     - long levels SL `entry × (1 − p)`, TP `entry × (1 + p)`, TRAIL `peak × (1 − p)`, ATR `entry − distance`, IBG_SL `ref × (1 − sl)`, IBG_TP `ref × (1 + tp)`; shorts mirrored;
     - at a gap point, a level already crossed fills at the point's price; inside a continuous segment, a level crossed in its direction (adverse for stops, favourable for profit exits) fills at the level;
     - several crossings in one segment: the level nearest the segment's start is reached first; a tie goes to stops (SL, TRAIL, ATR, IBG_SL in that order) over profit exits (TP, IBG_TP).
     - `watch` holds the extreme, `ref` and `prev`, which carry from bar to bar.
   - **`Overlays(rules, minutes=None)`**, the engine's `exits` hook:
     - `intrabar(simulation, t)` (step 3): for each open position on a tradable bar, the path of bar t: the minute path when `minutes` gives the bar's 1-minute bars, otherwise the daily path; with `minutes` set and no bars given, the position-day counts toward PATH_FALLBACK. An exit fills at its price with slippage, commission and tax like any fill; when a lock blocks it, it becomes a pending order with its reason (§10.5).
     - `at_close(simulation, t)`: MAX_HOLD. The entry bar counts as bar 1; at the close of bar N the exit becomes an order, which fills at the next open under NEXT_OPEN and at this close under SAME_CLOSE.
     - Prices are in the run's price mode. The ATR stop's distance is `mult × ATR(n)` on adjusted bars at the entry's signal bar, converted to the raw scale in REALISTIC mode.
     - `minutes` is a `MinutePaths` source: `DailyMinutePaths(sessions)` serves the daily timeline from features.py's `SessionBars` (only days with coverage OK); prompt 11 adds the minute-timeline source.
   - **Re-entry:** after an overlay exit the symbol's last executed target stays the strategy's current target, so the position re-enters only when the target changes, or at the next rebalance bar for WEIGHTS (§10.8). The engine already keeps those targets; an overlay exit also cancels the symbol's pending order.
2. **`btlab/engine.py`** changes:
   - Orders remember their signal bar; open trades their entry bar, signal bar and exit levels (the trailing extreme, ibguard `ref` and `prev`, and the ATR distance). A share event divides all of them by r with the entry price (§10.7); a cash dividend leaves them.
   - `Simulation.exit_position(t, j, price, reason)` fills a whole position at a price, or leaves a pending order with the reason when it cannot fill.
   - `at_close` runs after the strategy's evaluation, so a due MAX_HOLD exit replaces the strategy's order for that symbol at the same close (an overlay applies whatever the strategy, §10.8); SAME_CLOSE fills still come after both.
   - `EngineResult.flags` becomes a mapping from flag to its detail (None, or PATH_FALLBACK's count), because §10.11's flags carry details.
3. **Tests** (`tests/test_exits.py`), hand-made bars, long and short:
   - each overlay's level and fill: at the level inside a segment, at the open after a gap; TRAIL from a moving peak; ATR in the raw scale; MAX_HOLD under both fill timings (N = 1 and N = 3);
   - the daily path for up and down bars; the minute path with a gap between two minutes;
   - several overlays in one segment (the earliest wins) and a tie (the stop wins);
   - the ibguard vectors L1 to L10 and S1 to S5 through `ibguard_step`, with `ref` after each point and the exit;
   - levels divided by r on a split, unchanged on a dividend;
   - re-entry only after the strategy's target changes (DIRECTION) or at the next rebalance bar (WEIGHTS);
   - PATH_FALLBACK counting the position-days without minute bars;
   - an exit blocked by a lock waits for the next tradable bar;
   - exit fills pay slippage, commission and tax; positions opened at this bar's open are checked on this bar's path.
4. Run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **MAX_HOLD of 1 bar under SAME_CLOSE** exits at the next close: the entry fills at the close after the max-hold check, so "the close of the first bar" has passed.
- **A due MAX_HOLD exit wins over the strategy's order** for that symbol at the same close.
- **An exit that a limit lock blocks** stays pending with its reason and fills like any pending order.
- **A position increased later** keeps its levels on the average entry price; its trailing extreme and ibguard `ref` carry on from the first entry.

## 2. Extension points

- **An overlay** is one entry in the level list of `walk` and one field of `ExitRules`.
- **A path source** is any object with `bars(t, j)`; prompt 11 adds one for minute timelines.

## 3. Refactor seams

- Paths, the ibguard rule and the walk are pure functions of plain values; `Overlays` only connects them to the engine.
- The engine knows nothing of overlays beyond the `Exits` hook, `exit_position` and the level fields of its open trades.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-10 holds.** `.venv/bin/python -m pytest -q`: 677 passed (also with `-X dev -W error`). The vectors L1 to L10 and S1 to S5 pass through `ibguard_step` and, as gap points, through `walk`.
- The engine's open-trade class is now public (`OpenTrade`), since exits.py reads and sets its levels.
- The engine-test builders moved to `tests/builders.py` for reuse.
- Timing at bench size (50 symbols × 7,800 bars, MA cross with the ibguard exit, synthetic splits and dividends): 1.4 s on this VM (§24.3 case 1 allows 5 s on the owner's PC, metrics and loading not included yet).
- **For prompts 8 and 11:** build `Overlays(ExitRules(...), minutes)` per run, from the run's `exits` (percentages as given) and `intrabar_path`: `minutes=None` for DAILY, `DailyMinutePaths(sessions)` for MINUTE on the daily timeline (SessionBars with multiplier 1 in REALISTIC, `Aligned.multiplier` in ADJUSTED). PATH_FALLBACK arrives in `EngineResult.flags` with its count.
