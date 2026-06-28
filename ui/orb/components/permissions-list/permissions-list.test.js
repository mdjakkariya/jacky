import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: { permissions: vi.fn(), openPermission: vi.fn().mockResolvedValue({}) },
}));
import { daemon } from "../../lib/daemon.js";
import "./permissions-list.js";

beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '<permissions-list id="permsList"></permissions-list>'; });

it("renders one row per permission with the status badge", async () => {
  daemon.permissions.mockResolvedValue({ permissions: [{ key: "mic", label: "Microphone", description: "for voice", status: "granted" }] });
  const list = document.getElementById("permsList");
  await list.load();
  const row = list.querySelector(".perm-row");
  expect(row.querySelector(".label").textContent).toBe("Microphone");
  expect(row.querySelector(".badge").className).toBe("badge granted");
  expect(row.querySelector(".badge").textContent).toBe("Granted");
});

it("Open Settings calls openPermission with the key", async () => {
  daemon.permissions.mockResolvedValue({ permissions: [{ key: "mic", label: "Microphone", description: "", status: "needed" }] });
  const list = document.getElementById("permsList");
  await list.load();
  list.querySelector(".btn").click();
  expect(daemon.openPermission).toHaveBeenCalledWith("mic");
});
