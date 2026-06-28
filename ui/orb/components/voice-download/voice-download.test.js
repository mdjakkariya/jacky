import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: { voiceStatus: vi.fn(), voiceDownload: vi.fn().mockResolvedValue({}), wsUrl: "ws://x/ws" },
}));
import { daemon } from "../../lib/daemon.js";
import "./voice-download.js";

function mount() {
  document.body.innerHTML = `
    <voice-download id="voiceSetupCard">
      <div class="desc" id="voiceStatusDesc">…</div>
      <span class="badge" id="voiceBadge">…</span>
      <button id="voiceDownloadBtn">Download</button>
      <div id="voiceProgressRow" style="display:none">
        <div id="voiceProgressLabel"></div>
        <div id="voiceProgressBar"></div>
      </div>
    </voice-download>`;
  return document.querySelector("voice-download");
}

beforeEach(() => { vi.clearAllMocks(); });

it("shows Ready when models are downloaded", async () => {
  daemon.voiceStatus.mockResolvedValue({ ready: true });
  const el = mount();
  await el.loadStatus();
  expect(document.getElementById("voiceBadge").textContent).toBe("Ready");
  expect(document.getElementById("voiceBadge").className).toBe("badge granted");
  expect(document.getElementById("voiceDownloadBtn").style.display).toBe("none");
});

it("shows what's missing when not installed", async () => {
  daemon.voiceStatus.mockResolvedValue({ ready: false, needed: ["stt", "wake"], models: { wake: true } });
  const el = mount();
  await el.loadStatus();
  expect(document.getElementById("voiceBadge").textContent).toBe("Not installed");
  expect(document.getElementById("voiceStatusDesc").textContent).toContain("stt");
});
