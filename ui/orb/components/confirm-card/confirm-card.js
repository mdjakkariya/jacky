/** Chat confirmation card (created dynamically in the transcript, so a module rather
 *  than a custom element). Tiers tone to read/write/danger; optional buttons for
 *  multiple choices. Answer posts {value} via lib/daemon. Moved from chat.html. */
import { daemon } from "../../lib/daemon.js";

export function clearConfirm(log) {
  const c = log.querySelector("#confirm-card");
  if (c) c.remove();
}

export function showConfirm(log, prompt, kind, options, meta) {
  clearConfirm(log);
  kind = kind || "danger";
  meta = meta || {};
  const hasOpts = !!(options && options.length);
  const card = document.createElement("div"); card.className = "confirm " + kind; card.id = "confirm-card";
  const head = kind === "read" ? "Allow access" : (kind === "write" ? "Allow change" : (kind === "network" ? "Allow network action" : "⚠️ Just checking"));
  const yes = kind === "danger" ? "Yes, do it" : "Allow";
  const yesCls = kind === "danger" ? "btn danger" : "btn primary";
  const no = kind === "danger" ? "Cancel" : "Not now";
  card.innerHTML = '<div class="h"></div><div class="b"></div>';
  card.querySelector(".h").textContent = head;
  card.querySelector(".b").textContent = prompt || "Do you want me to go ahead with this?";
  const row = document.createElement("div"); row.className = "row";
  if (hasOpts) {
    // One button per option (last is the primary), plus Cancel — clearer than a dropdown.
    const cancel = document.createElement("button"); cancel.className = "btn"; cancel.setAttribute("data-v", "no"); cancel.textContent = no;
    row.appendChild(cancel);
    options.forEach((o, i) => {
      const b = document.createElement("button");
      b.className = i === options.length - 1 ? yesCls : "btn";
      b.setAttribute("data-v", o.value); b.textContent = o.label;
      row.appendChild(b);
    });
  } else {
    const noBtn = document.createElement("button"); noBtn.className = "btn"; noBtn.setAttribute("data-v", "no"); noBtn.textContent = no;
    const yesBtn = document.createElement("button"); yesBtn.className = yesCls; yesBtn.setAttribute("data-v", "yes"); yesBtn.textContent = yes;
    row.appendChild(noBtn); row.appendChild(yesBtn);
  }
  card.appendChild(row);
  // Add network-specific disclosure rows before the button row
  if (kind === "network" && (meta.serverLabel || meta.egress)) {
    const rowContainer = card.querySelector(".row");
    if (meta.serverLabel) {
      const kvConn = document.createElement("div"); kvConn.className = "kv";
      const kvK = document.createElement("span"); kvK.className = "k"; kvK.textContent = "Connection";
      const kvV = document.createElement("span"); kvV.className = "srvbadge"; kvV.textContent = meta.serverLabel;
      kvConn.appendChild(kvK);
      kvConn.appendChild(kvV);
      card.insertBefore(kvConn, rowContainer);
    }
    if (meta.egress) {
      const kvPath = document.createElement("div"); kvPath.className = "kv";
      const kvK = document.createElement("span"); kvK.className = "k"; kvK.textContent = "Data path";
      const kvV = document.createElement("span"); kvV.className = "egress"; kvV.textContent = "↗ " + meta.egress;
      kvPath.appendChild(kvK);
      kvPath.appendChild(kvV);
      card.insertBefore(kvPath, rowContainer);
    }
  }
  card.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      daemon.confirm({ value: b.getAttribute("data-v") });
      card.remove();
    });
  });
  log.appendChild(card); if (log.scroll) log.scroll();
  return card;
}
