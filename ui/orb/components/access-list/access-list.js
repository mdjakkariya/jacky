/** Folders & access manager: lists granted folders (with revoke) and grants a new one
 *  by path + write flag. Wraps the access card; emits a `status` event for the page's
 *  status line. */
import { daemon } from "../../lib/daemon.js";

export class AccessList extends HTMLElement {
  connectedCallback() {
    // Delegate + lazy child lookups: happy-dom (and some dynamic-create paths) fire
    // connectedCallback before light-DOM children are attached, so never cache refs here.
    this.addEventListener("click", (e) => { if (e.target.closest("#accessAddBtn")) this.#grant(); });
    queueMicrotask(() => this.load());
  }

  async load() {
    const box = this.querySelector("#accessList"); if (!box) return;
    try {
      const r = await daemon.access();
      const gs = (r && r.grants) || [];
      if (!gs.length) { box.textContent = "No folders granted yet."; return; }
      box.innerHTML = "";
      gs.forEach((g) => {
        const row = document.createElement("div"); row.className = "row";
        const info = document.createElement("span"); info.className = "grow";
        const name = document.createElement("div"); name.className = "label"; name.style.fontWeight = "400"; name.textContent = g.path;
        const mode = document.createElement("div"); mode.className = "desc"; mode.textContent = g.mode === "write" ? "read & write" : "read only";
        info.appendChild(name); info.appendChild(mode);
        const btn = document.createElement("button"); btn.className = "btn"; btn.textContent = "Revoke";
        btn.addEventListener("click", () => this.#revoke(g.path));
        row.appendChild(info); row.appendChild(btn); box.appendChild(row);
      });
    } catch (e) { box.textContent = "Couldn't load the access list."; }
  }

  async #grant() {
    const pathEl = this.querySelector("#accessPath"), writeEl = this.querySelector("#accessWrite");
    const p = (pathEl.value || "").trim(); if (!p) return;
    try {
      const r = await daemon.grantAccess(p, writeEl.checked);
      if (!r.ok) { this.#status(r.error || "Couldn't grant access", true); return; }
      pathEl.value = ""; writeEl.checked = false; this.load();
    } catch (e) { this.#status("Couldn't grant access", true); }
  }

  async #revoke(path) {
    try { await daemon.revokeAccess(path); } catch (e) {}
    this.load();
  }

  #status(msg, isError) {
    this.dispatchEvent(new CustomEvent("status", { detail: { msg, isError }, bubbles: true }));
  }
}
customElements.define("access-list", AccessList);
