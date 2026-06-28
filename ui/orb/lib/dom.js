/** Tiny DOM helpers shared across pages/components. No framework. */
export const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "className") node.className = v;
    else if (k === "textContent") node.textContent = v;
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  if (children != null) {
    for (const c of [].concat(children)) {
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }
  return node;
}

export function on(target, type, handler, opts) {
  target.addEventListener(type, handler, opts);
  return () => target.removeEventListener(type, handler, opts);
}

/** Point a popover's caret at the horizontal center of its trigger. The popover's
 *  `::before` caret reads its x from the `--caret-x` custom property; this sets it,
 *  clamped so the caret stays on the card. Call AFTER the popover is visible (it needs
 *  layout). Triggers that move (e.g. the folder chip shifting when the context meter
 *  appears) stay anchored because we measure on open. */
export function pointCaretAt(popover, trigger) {
  if (!popover || !trigger) return;
  const p = popover.getBoundingClientRect();
  const t = trigger.getBoundingClientRect();
  const x = Math.max(12, Math.min(p.width - 16, t.left + t.width / 2 - p.left));
  popover.style.setProperty("--caret-x", x + "px");
}
