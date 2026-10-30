// Results, Optimize and Live arrive with their prompts; until then the tab says so.

import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";

export function render(root, context) {
  root.append(h("h2", {text: t(`view.${context.name}`)}), h("p", {class: "quiet", text: t("later.coming")}));
}
