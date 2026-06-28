/** Active-folder chip + detail modal in the chat header. Controller module (the chip
 *  #folder and the modal #folderDetail are separate elements). Driven by the "workspace"
 *  WS frame and GET /workspace. Returns { refresh, renderFromEvent }. Moved from chat.html. */
import { $ } from "../../lib/dom.js";
import { daemon } from "../../lib/daemon.js";
import { revealInFinder, pickFolder } from "../../lib/tauri.js";

export function setupFolderChip() {
  let workspacePath = "";

  // Single source of truth: fetch /workspace and update BOTH the chip and the modal.
  async function refresh() {
    try {
      const w = await daemon.workspace();
      workspacePath = w.path || "";
      const chip = $("folder"); if (chip) { chip.style.display = workspacePath ? "inline-flex" : "none"; chip.classList.toggle("hidden", !workspacePath); }
      const nm = $("folderName"); if (nm) nm.textContent = w.name || "";
      const fp = $("folderPath"); if (fp) fp.textContent = w.path || "(none)";
      const grants = (w.grants || []).map((g) => g.path + " (" + g.mode + ")");
      const fg = $("folderGrants"); if (fg) fg.textContent = grants.length ? ("Granted: " + grants.join(", ")) : "";
    } catch (e) {}
  }

  // From the WS "workspace" event — update the chip only (modal may not be open).
  function renderFromEvent(m) {
    workspacePath = m.path || "";
    const chip = $("folder"); if (!chip) return;
    chip.style.display = workspacePath ? "inline-flex" : "none";
    chip.classList.toggle("hidden", !workspacePath);
    const nm = $("folderName"); if (nm) nm.textContent = m.name || "";
  }

  function openDetail() {
    const d = $("folderDetail"); if (!d) return;
    if (!d.classList.contains("hidden")) return; // already open
    refresh(); // populate/refresh path + grants before showing
    d.classList.remove("hidden");
    const chip = $("folder"); if (chip) chip.setAttribute("aria-expanded", "true");
  }
  function closeDetail() {
    const d = $("folderDetail"); if (!d) return;
    d.classList.add("hidden");
    const chip = $("folder"); if (chip) chip.setAttribute("aria-expanded", "false");
  }
  function toggleDetail() {
    const d = $("folderDetail"); if (!d) return;
    if (d.classList.contains("hidden")) openDetail(); else closeDetail();
  }
  function reveal() { if (workspacePath) revealInFinder(workspacePath); }
  async function changeFolder() {
    let picked; try { picked = await pickFolder(); } catch (e) { return; }
    if (!picked) return; // cancelled
    try { await daemon.setWorkspace(picked); } catch (e) {}
    await refresh(); // reflect the new folder without depending on the WS event
  }

  const chip = $("folder");
  if (chip) {
    chip.addEventListener("click", (e) => { e.stopPropagation(); toggleDetail(); });
    chip.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); toggleDetail(); } });
  }
  if ($("folderReveal")) $("folderReveal").addEventListener("click", (e) => { e.stopPropagation(); reveal(); });
  if ($("folderChange")) $("folderChange").addEventListener("click", (e) => { e.stopPropagation(); changeFolder(); });

  // Outside-click closes the modal.
  document.addEventListener("click", (e) => {
    const d = $("folderDetail"), c = $("folder");
    if (!d || d.classList.contains("hidden")) return;
    if ((d && d.contains(e.target)) || (c && c.contains(e.target))) return;
    closeDetail();
  });
  // Escape closes only the modal (capture phase, before the drawer's keydown).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const d = $("folderDetail");
      if (d && !d.classList.contains("hidden")) { e.stopPropagation(); closeDetail(); }
    }
  }, true);

  return { refresh, renderFromEvent };
}
