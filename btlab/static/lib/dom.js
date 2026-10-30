// Elements are built with DOM APIs and textContent only: server data never become HTML (SPEC.md §20.1).

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "on") for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
    else if (key === "data") Object.assign(node.dataset, value);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function field(label, control, path) {
  // A labelled control; path is its JSON path, which server errors name (§19).
  return h("label", {class: "field", data: path ? {path} : undefined},
    h("span", {class: "field-label", text: label}), control, h("span", {class: "field-error", role: "alert"}));
}

export function stepper(value, {min, max, step = 1, integer = false, optional = false, onChange} = {}) {
  // A number input between − and + buttons of 44 px, for thumbs (§20.4).
  const input = h("input", {type: "number", inputmode: integer ? "numeric" : "decimal", step: String(step),
    min: min ?? undefined, max: max ?? undefined, value: value ?? ""});
  const read = () => (input.value === "" ? (optional ? null : NaN) : Number(input.value));
  const move = (sign) => {
    const current = input.value === "" ? Number(min ?? 0) : Number(input.value);
    let next = current + sign * Number(step);
    if (min !== undefined && min !== null) next = Math.max(Number(min), next);
    if (max !== undefined && max !== null) next = Math.min(Number(max), next);
    input.value = String(integer ? Math.round(next) : Number(next.toPrecision(12)));
    input.dispatchEvent(new Event("change"));
  };
  if (onChange) input.addEventListener("change", () => onChange(read()));
  const box = h("span", {class: "stepper"},
    h("button", {type: "button", class: "step", "aria-label": "−", text: "−", on: {click: () => move(-1)}}),
    input,
    h("button", {type: "button", class: "step", "aria-label": "+", text: "+", on: {click: () => move(1)}}));
  box.read = read;
  box.input = input;
  return box;
}

export function select(options, value, onChange) {
  // options: [value, label] pairs.
  const node = h("select", {}, options.map(([key, label]) => h("option", {value: key, text: label})));
  node.value = value ?? (options[0] ? options[0][0] : "");
  if (onChange) node.addEventListener("change", () => onChange(node.value));
  return node;
}

export function checkbox(checked, onChange) {
  const node = h("input", {type: "checkbox", class: "toggle"});
  node.checked = Boolean(checked);
  if (onChange) node.addEventListener("change", () => onChange(node.checked));
  return node;
}

export function button(label, onClick, kind = "") {
  return h("button", {type: "button", class: `button ${kind}`.trim(), text: label, on: {click: onClick}});
}

export function showErrors(root, errors, prefix = "") {
  // Puts each error under the field whose data-path matches it; returns the ones no field took.
  for (const node of root.querySelectorAll(".field-error")) node.textContent = "";
  for (const node of root.querySelectorAll(".invalid")) node.classList.remove("invalid");
  const left = [];
  for (const error of errors) {
    const path = (error.path || "").startsWith(prefix) ? error.path.slice(prefix.length) : error.path || "";
    const target = fieldFor(root, path);
    if (target) {
      target.classList.add("invalid");
      target.querySelector(".field-error").textContent = error.message;
    } else {
      left.push(error);
    }
  }
  return left;
}

function fieldFor(root, path) {
  // "symbols[2]" belongs to the "symbols" field; a deeper path to the nearest field that holds it.
  let candidate = path;
  while (candidate) {
    const found = root.querySelector(`[data-path="${CSS.escape(candidate)}"]`);
    if (found) return found;
    const cut = Math.max(candidate.lastIndexOf("."), candidate.lastIndexOf("["));
    candidate = cut > 0 ? candidate.slice(0, cut) : "";
  }
  return null;
}
