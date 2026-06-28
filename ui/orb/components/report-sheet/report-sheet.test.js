import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({ daemon: { report: vi.fn(), reportFile: vi.fn() } }));
vi.mock("../../lib/clipboard.js", () => ({ copyText: vi.fn().mockResolvedValue(true) }));
vi.mock("../../lib/tauri.js", () => ({ openExternal: vi.fn(), revealInFinder: vi.fn(), hasTauri: () => false }));
import { daemon } from "../../lib/daemon.js";
import { copyText } from "../../lib/clipboard.js";
import { setupReportSheet } from "./report-sheet.js";

function mount() {
  document.body.innerHTML = `
    <div id="reportBackdrop"></div>
    <div id="reportPane"><textarea id="reportOut"></textarea><span id="reportHint"></span><a id="revealReport"></a><button id="reportClose"></button></div>
    <span id="status"></span>
    <span id="reportActions" class="hidden"><button id="reportIssue"></button><button id="copyReport"><span id="copyTip"></span></button></span>
    <button id="raiseIssue"></button>`;
}

beforeEach(() => { vi.clearAllMocks(); mount(); });

it("open() builds the report, copies it, and reveals the pane", async () => {
  daemon.report.mockResolvedValue({ report: "REPORT BODY" });
  const sheet = setupReportSheet(() => {});
  await sheet.open();
  expect(document.getElementById("reportOut").value).toBe("REPORT BODY");
  expect(copyText).toHaveBeenCalledWith("REPORT BODY");
  expect(document.getElementById("reportPane").classList.contains("open")).toBe(true);
  expect(document.getElementById("reportActions").classList.contains("hidden")).toBe(false);
});

it("clicking the backdrop closes the pane", async () => {
  daemon.report.mockResolvedValue({ report: "X" });
  const sheet = setupReportSheet(() => {});
  await sheet.open();
  document.getElementById("reportBackdrop").click();
  expect(document.getElementById("reportPane").classList.contains("open")).toBe(false);
});
