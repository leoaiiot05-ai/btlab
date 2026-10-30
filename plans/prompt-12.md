# Prompt 12: Rule builder

Covers SPEC.md §8 (the operands a rule reads) and §9.6. Acceptance: AC-14.

## 1. Steps

1. **`btlab/rules.py`** (pure):
   - **The schema (§9.6).** A rule is `{"version": 1, "timeframe", "side", "params"?, "entry", "exit"}`. A node is `{"all": [...]}`, `{"any": [...]}`, `{"not": node}` or a condition `{"left", "op", "right", "within"? | "for"?}`. An operand is exactly one of `{"ind", "args"?, "out"?, "src"?, "tf"?, "shift"?}`, `{"value"}`, `{"series"}`, `{"feature", "args"?}` or `{"today"}`. `params` is a parameter schema of §9.2.
   - **Validation.** `validate_rule(rule, series=()) -> list[RuleIssue]`, every error with its code, JSON path (`entry.all[0].left.args.n`, the rule's root is `""`) and message:
     - UNKNOWN_INDICATOR: an `ind`, `feature` or `today` name the registry (§8.6) does not list under that kind;
     - BAD_ARGS: an operand's arguments: an unknown key or argument, a value of the wrong type or outside the registry's range, `src` or `out` that the operand does not take, `shift` outside 0–500, `within` or `for` outside 1–500 or both on one condition, a `tf` that is not a timeframe;
     - BAD_OP: `op` outside `gt`, `lt`, `ge`, `le`, `cross_above`, `cross_below`;
     - DEPTH: a logical node below the third logical level, at that node's path;
     - TF_MISMATCH: an operand not valid on the rule's timeline (the registry's timeframes; `feature` on the daily timeline only, `today` and VWAP on minute timelines only), and a `tf` other than the rule's own timeframe or, on a minute timeline, `1d`;
     - UNKNOWN_PARAM: `{"param": name}` naming no parameter of the rule;
     - UNKNOWN_SERIES: a `series` outside the names given (§8.5);
     - TOO_MANY_CONDITIONS: more than 32 conditions in `entry` and `exit` together, at the root;
     - INVALID_FIELD (the code §9.2 and §10.1 give such errors) for the document's own structure: not an object, an unknown or missing key, `version` other than 1, an unknown `timeframe` or `side`, an invalid `params` schema, a node that is not an object or an empty `all`/`any`.
   - **Parameters.** Every numeric field (an argument, `value`, `shift`, `within`, `for`) may be `{"param": name}`. The parameter must be a number (an `int` parameter for whole-number fields), and on a field with a range its `min` and `max` must lie inside it, so every value a run or an optimization can give is valid (BAD_ARGS otherwise). `schema(rule)` returns the parameters as `Param`s.
   - **Evaluation.** `directions(rule, params, view, series=None) -> np.ndarray`, bars × symbols of +1 or −1 (in position, by `side`) and 0 (flat), for a valid rule:
     - operands resolve through the registry with every argument at its default unless given, parameters substituted; `ind` reads the view's adjusted bars; with `"tf": "1d"` on a minute timeline it is computed on the run's daily bars and each bar takes the last completed session before its own (§8.4, `features.previous_session`); `shift` k moves an operand k bars later, counted in the operand's own bars; `feature` reads the view's minute bars by day (§8.2); `today` reads the minute timeline's bars (§8.3); `series` reads the aligned series given (§8.5), the same value for every symbol; `value` is a constant;
     - conditions compare whole arrays: any NaN makes a condition false; `cross_above` is `left > right` at t and `left ≤ right` at t − 1 (false on the first bar), `cross_below` mirrored; `within: n` is true when the condition held on at least one of the last n bars including t, `for: n` when it held on each of them (false before n bars exist); `all`, `any` and `not` combine them;
     - per symbol, flat → in position when `entry` is true, in position → flat when `exit` is true, from the first bar of the warm-up on, like the built-ins.
   - `RuleStrategy(rule, params)`: the run's strategy, called with the run's `View`, returning DIRECTION targets. It is plain data, so it pickles for a pool worker (prompt 13).
   - `operands_of(rule)`: every operand of a rule, for the loader (features need minute bars) and for prompt 20 (IN_SAMPLE_SERIES).
2. **What strategies read (`builtins.View`).**
   - `View.daily` becomes `Daily(days, bars)`: the run's adjusted daily bars with their days on minute timelines, which rules compute daily indicators on. `plugins.Bars.daily` keeps exposing them as before, each bar taking the previous session's daily bar (§8.4), computed from it; `MinuteRun.daily` goes.
   - `View.minute_bars`: on the daily timeline, each symbol's 1-minute bars by day, adjusted (`features.SessionBars`), when the strategy reads features (§8.2).
3. **`btlab/backtest.py`.**
   - `strategy.source` adds `rule`: `{"source": "rule", "id"?, "rule", "params"?}`. The rule's errors are listed under `strategy.rule.<path>` with their codes in the messages; `params` are checked against the rule's schema and stored complete; the run's `timeframe` must be the rule's; a short rule needs `allow_short` (§9.6). The stored configuration carries the complete rule (§10.1).
   - `strategy_factory` returns a `RuleStrategy`.
   - A daily run whose rule reads features loads the symbols' minute bars and coverage as the MINUTE intrabar path does, and its view carries them scaled to the adjusted series. A day without coverage OK gives NaN (§8.2), so its conditions are false.
4. **`btlab/store.py`.** Rules by unique name (`rules` table of §18), like lists: `create_rule(name, rule, updated_at) -> id` (a name in use raises `StoreError` with conflict), `update_rule(id, name, rule, updated_at) -> bool`, `delete_rule(id) -> bool`, `rules() -> list[RuleRow]` by name. `RuleRow(id, name, rule, updated_at)` in `records.py`.
5. **Tests** (`tests/test_rules.py`, AC-14, and the store's in `tests/test_store.py`):
   - every validation code with its path, all errors of a rule at once, and the valid example of §9.6;
   - 3 logical levels pass and 4 fail with DEPTH; 32 conditions pass and 33 fail;
   - parameter substitution, defaults, and the parameter type and range checks;
   - each operator at its boundaries (equality), `cross_above` and `cross_below`, `within` and `for`, NaN false, `shift`;
   - the state machine for long and short rules;
   - a rule equivalent to built-in A gives its directions wherever A gives one (flat where A has none), and identical backtest results;
   - a minute rule with `"tf": "1d"` reads the previous session; a minute rule with `today` operands; a daily rule with features, false on a day without coverage; a series operand; minute operands on the daily timeline fail with TF_MISMATCH;
   - truncation invariance on the daily timeline (indicators, features) and on a minute timeline (`tf: 1d`, `today`);
   - backtest configurations with rules (paths, codes, timeframe, shorts), a rule run through the stores, and a `RuleStrategy` that pickles;
   - the store's rule functions, with the name conflict.
6. Add `rules` to the pure modules of `tests/test_purity.py`; run `.venv/bin/python -m pytest -q` until it passes.

### Readings where the spec is silent

- **Error codes.** §9.6 lists the rule-specific codes. Errors in the document's own structure use INVALID_FIELD, the code §9.2 and §10.1 give such errors; everything inside an operand except its name and `tf` is BAD_ARGS.
- **`within` and `for`** exclude each other on one condition (§9.6 says "or").
- **"Any argument or value may be a parameter"** covers every numeric field: arguments, `value`, `shift`, `within` and `for`. `src`, `out` and `tf` stay literal.
- **`tf` on a minute timeline** may be the rule's own timeframe or `1d`; another minute timeframe is TF_MISMATCH, since §9.6 defines only `1d` there.
- **`shift` on a `"tf": "1d"` operand** counts daily bars, before §8.4 maps the value to the minute bars.
- **One transition per bar.** In position, `exit` true goes flat even when `entry` is true as well; flat, `entry` true enters even when `exit` is true as well.
- **Directions are 0 or ±1 on every bar.** A condition with NaN is false, so a rule stays flat where its operands are unknown; built-in A gives NaN (no instruction) there. Both mean no order when flat, so the backtests agree.
- **"A short rule needs a backtest that allows shorting"** is a configuration error at `allow_short`, not SHORTS_IGNORED.
- **`strategy.id` of a rule run** is optional: the stored rule's name when there is one.
- **External series** are validated against the names given and read from the aligned values given. Until prompt 20 registers the stored series, a backtest knows none, so a series operand fails with UNKNOWN_SERIES.

## 2. Extension points

- **An operator** is one entry of the comparison table.
- **An operand kind** is one entry of the operand keys and one resolver.
- **External series** (prompt 20): `validate_rule(rule, series)` takes their names and `directions(..., series)` their aligned values.

## 3. Refactor seams

- Validation and evaluation are separate; evaluation trusts a valid rule.
- Operands resolve only through the registry; rules.py holds no indicator code.
- Strategies receive everything through `View`; the loader decides what goes in it.

## 4. Deferred items (appended to TODO.md)

- None.

## Result

- **AC-14 holds.** `.venv/bin/python -m pytest -q`: 968 passed (also with `-X dev -W error`). `rules` joined the pure modules of `tests/test_purity.py`.
- Built as planned. `View.daily` is now `Daily(days, bars)`; plugins still see `bars.daily` aligned to the previous session, and the look-ahead check leaves the daily bars whole because they are read only through it.
- Identical operands (after parameter substitution) are computed once per evaluation, so entry and exit sharing indicators cost one computation.
- **For prompts 13, 15–17 and 20:** `RuleStrategy` pickles as plain data; `validate_rule(rule, series)` returns `RuleIssue(code, path, message)` for `POST /api/rules/validate` and RULE_INVALID; `schema(rule)` gives the parameters for optimization ranges; `operands_of(rule)` finds series operands for IN_SAMPLE_SERIES; `directions(rule, params, view, series)` takes the aligned series once prompt 20 loads them; the store has `create_rule`, `update_rule`, `delete_rule` and `rules()`.
