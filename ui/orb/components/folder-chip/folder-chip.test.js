import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({ daemon: { workspace: vi.fn(), setWorkspace: vi.fn() } }));
vi.mock("../../lib/tauri.js", () => ({ revealInFinder: vi.fn(), pickFolder: vi.fn() }));
import { daemon } from "../../lib/daemon.js";
import { setupFolderChip } from "./folder-chip.js";

function mount() {
  document.body.innerHTML = `
    <span id="folder" class="folder hidden"><span id="folderName"></span></span>
    <div id="folderDetail" class="folder-detail hidden">
      <div id="folderPath"></div><div id="folderGrants"></div>
      <button id="folderReveal"></button><button id="folderChange"></button>
    </div>`;
}
beforeEach(() => { vi.clearAllMocks(); mount(); });

it("renderFromEvent shows the chip with the folder name", () => {
  const fc = setupFolderChip();
  fc.renderFromEvent({ path: "/a/b", name: "b" });
  const chip = document.getElementById("folder");
  expect(chip.classList.contains("hidden")).toBe(false);
  expect(document.getElementById("folderName").textContent).toBe("b");
});

it("renderFromEvent with no path hides the chip", () => {
  const fc = setupFolderChip();
  fc.renderFromEvent({ path: "", name: "" });
  expect(document.getElementById("folder").classList.contains("hidden")).toBe(true);
});

it("clicking the chip opens the modal (and refreshes from /workspace)", async () => {
  daemon.workspace.mockResolvedValue({ path: "/a/b", name: "b", grants: [{ path: "/a/b", mode: "read" }] });
  const fc = setupFolderChip();
  document.getElementById("folder").click();
  expect(document.getElementById("folderDetail").classList.contains("hidden")).toBe(false);
  await Promise.resolve();
  expect(daemon.workspace).toHaveBeenCalled();
});
