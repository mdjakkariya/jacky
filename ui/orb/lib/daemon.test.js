import { describe, it, expect, vi, beforeEach } from "vitest";

// Import fresh each test so the singleton's derived base reflects the stubbed location.
async function freshDaemon() {
  vi.resetModules();
  return (await import("./daemon.js")).daemon;
}

beforeEach(() => {
  // Default location: no query params.
  Object.defineProperty(window, "location", { value: new URL("http://tauri.localhost/chat.html"), writable: true, configurable: true });
});

describe("base/ws derivation", () => {
  it("defaults to loopback when no params", async () => {
    const d = await freshDaemon();
    expect(d.base).toBe("http://127.0.0.1:8765");
    expect(d.wsUrl).toBe("ws://127.0.0.1:8765/ws");
  });
  it("honors ?api= (settings)", async () => {
    window.location = new URL("http://tauri.localhost/settings.html?api=http://127.0.0.1:9000");
    const d = await freshDaemon();
    expect(d.base).toBe("http://127.0.0.1:9000");
    expect(d.wsUrl).toBe("ws://127.0.0.1:9000/ws");
  });
  it("honors ?ws= (orb)", async () => {
    window.location = new URL("http://tauri.localhost/index.html?ws=ws://localhost:8765/ws");
    const d = await freshDaemon();
    expect(d.base).toBe("http://localhost:8765");
  });
});

describe("get/post", () => {
  it("get parses JSON", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) });
    await expect(d.get("/healthz")).resolves.toEqual({ ok: true });
    expect(global.fetch).toHaveBeenCalledWith("http://127.0.0.1:8765/healthz");
  });
  it("post sends JSON body", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.post("/chat", { text: "hi" });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/chat");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ text: "hi" });
  });
  it("confirm posts the caller's exact payload shape", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.confirm({ value: "yes" });
    await d.confirm({ answer: true });
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({ value: "yes" });
    expect(JSON.parse(global.fetch.mock.calls[1][1].body)).toEqual({ answer: true });
  });
});

describe("on() dispatch", () => {
  it("routes a parsed message to its type handler and unsubscribes", async () => {
    const d = await freshDaemon();
    const seen = [];
    const off = d.on("context", (m) => seen.push(m));
    d._dispatch({ data: JSON.stringify({ type: "context", pct: 50 }) });
    expect(seen).toEqual([{ type: "context", pct: 50 }]);
    off();
    d._dispatch({ data: JSON.stringify({ type: "context", pct: 60 }) });
    expect(seen.length).toBe(1);
  });
  it("ignores non-JSON frames", async () => {
    const d = await freshDaemon();
    expect(() => d._dispatch({ data: "not json" })).not.toThrow();
  });
});
