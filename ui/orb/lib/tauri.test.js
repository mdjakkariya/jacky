import { describe, it, expect, vi, beforeEach } from "vitest";
import { hasTauri, invoke, openExternal, appVersion } from "./tauri.js";

beforeEach(() => { delete window.__TAURI__; });

describe("without Tauri", () => {
  it("hasTauri is false", () => { expect(hasTauri()).toBe(false); });
  it("invoke resolves undefined and does not throw", async () => {
    await expect(invoke("anything")).resolves.toBeUndefined();
  });
  it("appVersion falls back to 0.0.0", async () => { expect(await appVersion()).toBe("0.0.0"); });
});

describe("with Tauri", () => {
  it("invoke delegates to core.invoke", async () => {
    const core = { invoke: vi.fn().mockResolvedValue("ok") };
    window.__TAURI__ = { core };
    expect(hasTauri()).toBe(true);
    await expect(invoke("ping", { a: 1 })).resolves.toBe("ok");
    expect(core.invoke).toHaveBeenCalledWith("ping", { a: 1 });
  });
  it("openExternal forwards the url", async () => {
    const core = { invoke: vi.fn().mockResolvedValue(undefined) };
    window.__TAURI__ = { core };
    await openExternal("https://x.test");
    expect(core.invoke).toHaveBeenCalledWith("open_external", { url: "https://x.test" });
  });
});
