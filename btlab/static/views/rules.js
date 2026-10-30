// The rule builder: a tree of groups and conditions, the rule's parameters, live validation, saving by name and a
// read-only JSON view (SPEC.md §20.2 item 4, §9.6).

import {get, post, put} from "../lib/api.js";
import {button, clear, field, h, select, showErrors, stepper} from "../lib/dom.js";
import {code, errorText, t} from "../lib/i18n.js";

const TIMEFRAMES = ["1d", "1m", "5m", "15m", "30m", "60m"];
const OPS = ["gt", "lt", "ge", "le", "cross_above", "cross_below"];
const KINDS = ["ind", "feature", "today", "series", "value"];
const SOURCES = ["open", "high", "low", "close", "volume"];
const PARAM_TYPES = ["int", "float", "bool", "choice"];
const MAX_DEPTH = 3;
const WINDOW = {min: 1, max: 500, step: 1, integer: true};
const VALIDATE_AFTER_MS = 400;

export function render(root, context) {
  const message = h("p", {class: "message", role: "alert"});
  const body = h("div", {class: "rule-builder"});
  root.append(h("h2", {text: t("rules.title")}), button(t("rules.back"), () => context.go("backtest")), body, message);
  Promise.all([get("/api/indicators"), get("/api/rules")]).then(([operands, saved]) => {
    const state = {rule: context.draftRule ? structuredClone(context.draftRule) : blank(), id: null, name: "", operands, saved,
      timer: null, context, message};
    draw(body, state);
  }).catch((error) => {
    message.textContent = errorText(error);
  });
}

function blank() {
  return {version: 1, timeframe: "1d", side: "long", params: {}, entry: {all: [condition()]}, exit: {all: [condition("cross_below")]}};
}

function condition(op = "cross_above") {
  return {left: {ind: "SMA", args: {n: 20}}, op, right: {ind: "SMA", args: {n: 50}}};
}

function draw(root, state) {
  // Every change redraws the builder from the rule and validates it shortly after (§19: POST /api/rules/validate).
  const redraw = () => {
    state.context.draftRule = state.rule;
    draw(root, state);
  };
  clear(root);
  const rule = state.rule;
  root.append(
    header(state, redraw),
    h("fieldset", {class: "group"}, h("legend", {text: t("rules.settings")}),
      field(t("rules.timeframe"), select(TIMEFRAMES.map((value) => [value, code("timeframe", value)]), rule.timeframe,
        (value) => { rule.timeframe = value; redraw(); }), "timeframe"),
      field(t("rules.side"), select(["long", "short"].map((value) => [value, code("side", value)]), rule.side,
        (value) => { rule.side = value; redraw(); }), "side")),
    params(rule, redraw),
    h("fieldset", {class: "group"}, h("legend", {text: t("rules.entry")}), nodeView(rule.entry, "entry", 0, state, redraw,
      (node) => { rule.entry = node; })),
    h("fieldset", {class: "group"}, h("legend", {text: t("rules.exit")}), nodeView(rule.exit, "exit", 0, state, redraw,
      (node) => { rule.exit = node; })),
    h("ul", {class: "errors"}),
    h("details", {}, h("summary", {text: t("rules.json")}), h("pre", {class: "json", text: JSON.stringify(rule, null, 2)})));
  clearTimeout(state.timer);
  state.timer = setTimeout(() => validate(root, state), VALIDATE_AFTER_MS);
}

function header(state, redraw) {
  const name = h("input", {type: "text", value: state.name});
  name.addEventListener("change", () => { state.name = name.value; });
  const saved = select([["", t("rules.choose")], ...state.saved.map((item) => [String(item.id), item.name])], state.id ? String(state.id) : "",
    (value) => {
      const found = state.saved.find((item) => String(item.id) === value);
      if (!found) return;
      Object.assign(state, {rule: structuredClone(found.rule), id: found.id, name: found.name});
      redraw();
    });
  return h("div", {class: "inline"},
    field(t("rules.name"), name, "name"),
    field(t("rules.saved"), saved),
    button(t("rules.save"), () => save(state, redraw), "primary"),
    button(t("rules.new"), () => {
      Object.assign(state, {rule: blank(), id: null, name: ""});
      redraw();
    }),
    button(t("rules.use"), () => {
      state.context.draftRule = state.rule;
      state.context.go("backtest");
    }));
}

async function save(state, redraw) {
  try {
    const body = {name: state.name, rule: state.rule};
    if (state.id) {
      await put(`/api/rules/${state.id}`, body);
    } else {
      state.id = (await post("/api/rules", body)).id;
    }
    state.saved = await get("/api/rules");
    state.message.textContent = t("rules.saved_message", {name: state.name});
    redraw();
  } catch (error) {
    state.message.textContent = errorText(error);
  }
}

async function validate(root, state) {
  try {
    const found = await post("/api/rules/validate", {rule: state.rule});
    const errors = found.errors.map((issue) => ({path: issue.path, message: `${code("rule", issue.code)} — ${issue.message}`}));
    const left = showErrors(root, errors);
    const list = root.querySelector(".errors");
    if (list) {
      clear(list);
      for (const item of left) list.append(h("li", {text: item.path ? `${item.path}: ${item.message}` : item.message}));
      if (found.ok) list.append(h("li", {class: "ok", text: t("rules.valid")}));
    }
  } catch (error) {
    state.message.textContent = errorText(error);
  }
}

function params(rule, redraw) {
  // The rule's parameters, a §9.2 schema; arguments and values can refer to them.
  const rows = Object.entries(rule.params).map(([name, param]) => {
    const rename = h("input", {type: "text", value: name});
    rename.addEventListener("change", () => {
      const renamed = {};
      for (const [key, value] of Object.entries(rule.params)) renamed[key === name ? rename.value : key] = value;
      rule.params = renamed;
      redraw();
    });
    const numbers = ["default", "min", "max", "step"].filter((key) => param.type === "int" || param.type === "float" || key === "default")
      .map((key) => field(t(`rules.param_${key}`), param.type === "bool" && key === "default"
        ? select([["true", "true"], ["false", "false"]], String(param.default), (value) => { param.default = value === "true"; })
        : param.type === "choice" && key === "default"
          ? textInput(param.default, (value) => { param.default = value; })
          : stepper(param[key], {optional: key !== "default", step: param.type === "int" ? 1 : 0.1, integer: param.type === "int",
            onChange: (value) => { param[key] = value; redraw(); }}), `params.${name}.${key}`));
    return h("div", {class: "inline param"},
      field(t("rules.param_name"), rename, `params.${name}`),
      field(t("rules.param_type"), select(PARAM_TYPES.map((value) => [value, value]), param.type, (value) => {
        rule.params[name] = value === "bool" ? {type: value, default: false}
          : value === "choice" ? {type: value, default: "", choices: [""]} : {type: value, default: 1, min: 1, max: 100, step: 1};
        redraw();
      })),
      param.type === "choice" ? field(t("rules.param_choices"), textInput((param.choices || []).join(", "), (value) => {
        param.choices = value.split(",").map((item) => item.trim());
        redraw();
      })) : null,
      numbers,
      button("×", () => { delete rule.params[name]; redraw(); }, "danger"));
  });
  return h("fieldset", {class: "group", data: {path: "params"}}, h("legend", {text: t("rules.params")}), rows,
    button(t("rules.add_param"), () => {
      let index = Object.keys(rule.params).length + 1;
      while (`p${index}` in rule.params) index += 1;
      rule.params[`p${index}`] = {type: "int", default: 10, min: 2, max: 100, step: 1};
      redraw();
    }), h("span", {class: "field-error", role: "alert"}));
}

function nodeView(node, path, depth, state, redraw, replace, removeSelf) {
  const kind = ["all", "any", "not"].find((key) => key in node);
  if (!kind) return conditionView(node, path, state, redraw, removeSelf);
  const children = kind === "not" ? [node.not] : node[kind];
  const childPath = (index) => (kind === "not" ? `${path}.not` : `${path}.${kind}[${index}]`);
  const regroup = (value) => {
    const members = value === "not" ? {not: children[0] || condition()} : {[value]: kind === "not" ? [node.not] : children};
    replace(members);
    redraw();
  };
  return h("div", {class: `node depth-${depth}`, data: {path}},
    h("div", {class: "inline"},
      select(["all", "any", "not"].map((value) => [value, t(`rules.group_${value}`)]), kind, regroup),
      removeSelf ? button("×", () => { removeSelf(); redraw(); }, "danger") : null,
      h("span", {class: "field-error", role: "alert"})),
    children.map((child, index) => nodeView(child, childPath(index), depth + 1, state, redraw,
      (value) => { if (kind === "not") node.not = value; else node[kind][index] = value; },
      kind === "not" ? null : () => node[kind].splice(index, 1))),
    kind === "not" ? null : h("div", {class: "actions"},
      button(t("rules.add_condition"), () => { node[kind].push(condition()); redraw(); }),
      depth + 1 < MAX_DEPTH ? button(t("rules.add_group"), () => { node[kind].push({all: [condition()]}); redraw(); }) : null));
}

function conditionView(node, path, state, redraw, removeSelf) {
  const modifier = "within" in node ? "within" : "for" in node ? "for" : "";
  return h("div", {class: "condition", data: {path}},
    operandView(node.left, `${path}.left`, state, redraw, (value) => { node.left = value; }),
    field(t("rules.op"), select(OPS.map((value) => [value, code("op", value)]), node.op, (value) => { node.op = value; redraw(); }),
      `${path}.op`),
    operandView(node.right, `${path}.right`, state, redraw, (value) => { node.right = value; }),
    h("div", {class: "inline"},
      field(t("rules.modifier"), select([["", t("rules.modifier_none")], ["within", t("rules.within")], ["for", t("rules.for")]],
        modifier, (value) => {
          delete node.within;
          delete node.for;
          if (value) node[value] = 2;
          redraw();
        })),
      modifier ? numberOrParam(node[modifier], WINDOW, state, (value) => { node[modifier] = value; redraw(); }, `${path}.${modifier}`) : null,
      removeSelf ? button("×", () => { removeSelf(); redraw(); }, "danger") : null),
    h("span", {class: "field-error", role: "alert"}));
}

function operandView(operand, path, state, redraw, replace) {
  const kind = KINDS.find((key) => key in operand) || "value";
  const available = state.operands.filter((item) => item.kind === kind);
  const parts = [field(t("rules.operand_kind"), select(KINDS.map((value) => [value, code("operand", value)]), kind, (value) => {
    const first = state.operands.find((item) => item.kind === value);
    replace(value === "value" ? {value: 0} : first ? {[value]: first.name, ...(value === "today" || value === "series" ? {} : {args: {}})} : {value: 0});
    redraw();
  }))];
  if (kind === "value") {
    parts.push(numberOrParam(operand.value, {step: 0.1}, state, (value) => { operand.value = value; redraw(); }, `${path}.value`));
    return h("div", {class: "operand inline", data: {path}}, parts);
  }
  const found = available.find((item) => item.name === operand[kind]) || available[0];
  // F and COMPOSITE are fitted over their whole history (§5.6): the picker says so.
  const label = (item) => (item.in_sample ? t("rules.in_sample", {name: item.name}) : item.name);
  parts.push(field(t("rules.operand_name"), select(available.map((item) => [item.name, label(item)]), operand[kind], (value) => {
    replace({[kind]: value, ...(kind === "ind" || kind === "feature" ? {args: {}} : {})});
    redraw();
  }), `${path}.${kind}`));
  if (found && (kind === "ind" || kind === "feature")) {
    operand.args = operand.args || {};
    for (const [name, arg] of Object.entries(found.args)) {
      parts.push(numberOrParam(name in operand.args ? operand.args[name] : arg.default,
        {min: arg.min, max: arg.max, step: 1, integer: arg.type === "int"}, state,
        (value) => { operand.args[name] = value; redraw(); }, `${path}.args.${name}`, name));
    }
  }
  if (found && kind === "ind") {
    if (found.outputs.length) {
      parts.push(field(t("rules.out"), select(found.outputs.map((value) => [value, value]), operand.out || found.outputs[0],
        (value) => { operand.out = value; redraw(); }), `${path}.out`));
    }
    if (found.source) {
      parts.push(field(t("rules.src"), select(SOURCES.map((value) => [value, value]), operand.src || found.source,
        (value) => { operand.src = value; redraw(); }), `${path}.src`));
    }
    if (state.rule.timeframe !== "1d") {
      parts.push(field(t("rules.tf"), select([["", t("rules.tf_own")], ["1d", t("rules.tf_daily")]], operand.tf || "", (value) => {
        if (value) operand.tf = value; else delete operand.tf;
        redraw();
      }), `${path}.tf`));
    }
    parts.push(numberOrParam(operand.shift || 0, {min: 0, max: 500, step: 1, integer: true}, state, (value) => {
      if (value) operand.shift = value; else delete operand.shift;
      redraw();
    }, `${path}.shift`, t("rules.shift")));
  }
  return h("div", {class: "operand inline", data: {path}}, parts);
}

function numberOrParam(value, options, state, onChange, path, label = t("rules.value")) {
  // A number, or {"param": name} referring to one of the rule's parameters (§9.6).
  const names = Object.keys(state.rule.params);
  const referred = value && typeof value === "object" ? value.param : "";
  const which = select([["", t("rules.literal")], ...names.map((name) => [name, `{${name}}`])], referred,
    (name) => onChange(name ? {param: name} : options.min ?? 1));
  const number = referred ? null : stepper(value, {...options, onChange});
  return field(label, h("span", {class: "inline"}, number, names.length ? which : null), path);
}

function textInput(value, onChange) {
  const input = h("input", {type: "text", value: value ?? ""});
  input.addEventListener("change", () => onChange(input.value));
  return input;
}
