/** Chat confirmation card (created dynamically in the transcript, so a module rather
 *  than a custom element). Tiers tone to read/write/danger; optional <select> of
 *  choices. Answer posts {value} via lib/daemon. Moved from chat.html. */
import { daemon } from "../../lib/daemon.js";

export function clearConfirm(log) {
  const c = log.querySelector("#confirm-card");
  if (c) c.remove();
}

export function showConfirm(log, prompt, kind, options) {
  clearConfirm(log);
  kind = kind || "danger";
  const hasOpts = !!(options && options.length);
  const card = document.createElement("div"); card.className = "confirm " + kind; card.id = "confirm-card";
  const head = kind === "read" ? "Allow access" : (kind === "write" ? "Allow change" : "⚠️ Just checking");
  const yes = kind === "danger" ? "Yes, do it" : "Allow";
  const yesCls = kind === "danger" ? "btn danger" : "btn primary";
  const no = kind === "danger" ? "Cancel" : "Not now";
  card.innerHTML = '<div class="h"></div><div class="b"></div>'
    + '<div class="row"><button class="btn" data-v="no"></button>'
    + '<button class="' + yesCls + '" data-v="yes"></button></div>';
  card.querySelector(".h").textContent = head;
  card.querySelector(".b").textContent = prompt || "Do you want me to go ahead with this?";
  card.querySelector('[data-v="no"]').textContent = no;
  card.querySelector('[data-v="yes"]').textContent = yes;
  // Build the choices <select> with createElement (not innerHTML) so option text/values
  // can't break out of attribute context.
  let selEl = null;
  if (hasOpts) {
    selEl = document.createElement("select"); selEl.className = "confsel"; selEl.id = "confSel";
    options.forEach((o) => { const op = document.createElement("option"); op.value = o.value; op.textContent = o.label; selEl.appendChild(op); });
    card.insertBefore(selEl, card.querySelector(".row"));
  }
  card.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      let v = b.getAttribute("data-v");
      if (v === "yes" && hasOpts) { v = selEl ? selEl.value : "yes"; }
      daemon.confirm({ value: v });
      card.remove();
    });
  });
  log.appendChild(card); if (log.scroll) log.scroll();
  return card;
}
