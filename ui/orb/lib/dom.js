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
