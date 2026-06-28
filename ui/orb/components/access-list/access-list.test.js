import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: { access: vi.fn(), grantAccess: vi.fn(), revokeAccess: vi.fn().mockResolvedValue({}) },
}));
import { daemon } from "../../lib/daemon.js";
import "./access-list.js";

function mount() {
  document.body.innerHTML = `
    <access-list>
      <div id="accessList"></div>
      <input id="accessPath" />
      <input type="checkbox" id="accessWrite" />
      <button id="accessAddBtn">Grant</button>
    </access-list>`;
  return document.querySelector("access-list");
}

beforeEach(() => { vi.clearAllMocks(); });

it("renders granted folders with a revoke button", async () => {
  daemon.access.mockResolvedValue({ grants: [{ path: "/a/b", mode: "write" }] });
  const el = mount();
  await el.load();
  expect(el.querySelector("#accessList .label").textContent).toBe("/a/b");
  expect(el.querySelector("#accessList .desc").textContent).toBe("read & write");
  el.querySelector("#accessList .btn").click();
  expect(daemon.revokeAccess).toHaveBeenCalledWith("/a/b");
});

it("grant with an empty path is a no-op", async () => {
  daemon.access.mockResolvedValue({ grants: [] });
  const el = mount();
  el.querySelector("#accessAddBtn").click();
  expect(daemon.grantAccess).not.toHaveBeenCalled();
});

it("grant posts the path + write flag and clears the input", async () => {
  daemon.access.mockResolvedValue({ grants: [] });
  daemon.grantAccess.mockResolvedValue({ ok: true });
  const el = mount();
  el.querySelector("#accessPath").value = "/x/y";
  el.querySelector("#accessWrite").checked = true;
  el.querySelector("#accessAddBtn").click();
  await Promise.resolve(); await Promise.resolve();
  expect(daemon.grantAccess).toHaveBeenCalledWith("/x/y", true);
});
