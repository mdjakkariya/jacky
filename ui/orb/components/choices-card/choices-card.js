/** Chat action/choices card (created dynamically in the transcript). A title + up to 5
 *  items, each with action buttons: a `copy` action copies client-side; any other runs
 *  a named tool via lib/daemon (straight through the permission gate). Moved from chat.html. */
import { daemon } from "../../lib/daemon.js";
import { copyText } from "../../lib/clipboard.js";

function runAction(act, btn, statusEl) {
  if (act.copy != null) {
    copyText(String(act.copy));
    statusEl.textContent = "Copied to clipboard";
    return;
  }
  const row = btn.parentNode;
  row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  statusEl.textContent = "Working…";
  daemon.action(act.tool, act.args || {})
    .then((r) => { statusEl.textContent = (r && r.result) ? r.result : "Done"; })
    .catch(() => { statusEl.textContent = "Couldn't do that"; })
    .finally(() => { row.querySelectorAll("button").forEach((b) => { b.disabled = false; }); });
}

export function showChoices(log, m) {
  if (!m || !m.items || !m.items.length) return;
  const card = document.createElement("div"); card.className = "choices";
  const h = document.createElement("div"); h.className = "h"; h.textContent = m.title || "Choose one";
  card.appendChild(h);
  const top = m.items.slice(0, 5); // show the best few; a long list reads as clutter
  top.forEach((item) => {
    const it = document.createElement("div"); it.className = "it";
    const il = document.createElement("div"); il.className = "il"; il.textContent = item.label || "";
    it.appendChild(il);
    if (item.sublabel) { const is = document.createElement("div"); is.className = "is"; is.textContent = item.sublabel; it.appendChild(is); }
    const ia = document.createElement("div"); ia.className = "ia";
    const status = document.createElement("div"); status.className = "done";
    (item.actions || []).forEach((act) => {
      const b = document.createElement("button"); b.className = "btn"; b.textContent = act.label || "Do it";
      b.addEventListener("click", () => runAction(act, b, status));
      ia.appendChild(b);
    });
    it.appendChild(ia); it.appendChild(status); card.appendChild(it);
  });
  const more = m.items.length - top.length;
  if (more > 0) {
    const f = document.createElement("div"); f.className = "more";
    f.textContent = "+" + more + " more — tell me a more specific name to narrow it down.";
    card.appendChild(f);
  }
  log.appendChild(card); if (log.scroll) log.scroll();
}
