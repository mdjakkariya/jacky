/** macOS-permissions list: rows of label + status badge + "Open Settings" (which
 *  opens the right System Settings pane via the daemon). Container element itself. */
import { daemon } from "../../lib/daemon.js";

const PERM_LABEL = { granted: "Granted", needed: "Needed", unknown: "Unknown" };

export class PermissionsList extends HTMLElement {
  #loading = false;

  async load() {
    if (this.#loading) return; // guard against overlapping refreshes stacking rows
    this.#loading = true;
    let rows = [];
    try { rows = ((await daemon.permissions()).permissions) || []; }
    catch (e) { this.innerHTML = '<div class="perm-row"><span class="desc">Couldn\'t load — is Jack running?</span></div>'; this.#loading = false; return; }
    finally { this.#loading = false; }
    this.textContent = ""; // clear right before render (after the await) so concurrent calls can't stack
    rows.forEach((p) => {
      const row = document.createElement("div"); row.className = "perm-row";
      const info = document.createElement("span"); info.className = "grow";
      info.innerHTML = '<span class="label"></span><div class="desc"></div>';
      info.querySelector(".label").textContent = p.label;
      info.querySelector(".desc").textContent = p.description;
      const badge = document.createElement("span");
      badge.className = "badge " + p.status;
      badge.textContent = PERM_LABEL[p.status] || p.status;
      const btn = document.createElement("button"); btn.className = "btn ghost"; btn.textContent = "Open Settings";
      btn.addEventListener("click", () => daemon.openPermission(p.key));
      row.appendChild(info); row.appendChild(badge); row.appendChild(btn);
      this.appendChild(row);
    });
  }
}
customElements.define("permissions-list", PermissionsList);
