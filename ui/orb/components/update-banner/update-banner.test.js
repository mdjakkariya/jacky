import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/tauri.js", () => ({ appVersion: vi.fn().mockResolvedValue("1.0.0"), openExternal: vi.fn() }));
import { appVersion } from "../../lib/tauri.js";
import "./update-banner.js";

function mount() {
  document.body.innerHTML = `
    <update-banner id="updateBanner" class="upd hidden">
      <span class="u-text"></span>
      <span class="u-actions"><button class="u-link">What's new ↗</button><button class="u-dismiss">Later</button></span>
    </update-banner>`;
  return document.getElementById("updateBanner");
}

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });

it("shows the banner when a newer release exists", async () => {
  appVersion.mockResolvedValue("1.0.0");
  global.fetch = vi.fn().mockResolvedValue({ json: async () => ({ tag_name: "v1.1.0", html_url: "https://x" }) });
  const b = mount();
  await b.check();
  expect(b.classList.contains("hidden")).toBe(false);
  expect(b.querySelector(".u-text").textContent).toBe("Jack 1.1.0 is available");
});

it("stays hidden when already current", async () => {
  appVersion.mockResolvedValue("1.1.0");
  global.fetch = vi.fn().mockResolvedValue({ json: async () => ({ tag_name: "v1.1.0" }) });
  const b = mount();
  await b.check();
  expect(b.classList.contains("hidden")).toBe(true);
});

it("stays hidden when the version was dismissed", async () => {
  appVersion.mockResolvedValue("1.0.0");
  localStorage.setItem("jackUpdateDismissed", "1.1.0");
  global.fetch = vi.fn().mockResolvedValue({ json: async () => ({ tag_name: "v1.1.0" }) });
  const b = mount();
  await b.check();
  expect(b.classList.contains("hidden")).toBe(true);
});
