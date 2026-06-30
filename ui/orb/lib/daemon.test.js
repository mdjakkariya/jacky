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

describe("delete", () => {
  it("sends DELETE request with no body", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.delete("/mcp/servers/slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack");
    expect(opts.method).toBe("DELETE");
    expect(opts.body).toBeUndefined();
  });
});

describe("MCP methods", () => {
  it("mcpServers() calls GET /mcp/servers", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => [] });
    await d.mcpServers();
    const [url] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers");
  });
  it("addMcpServer(descriptor) calls POST /mcp/servers with descriptor as body", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.addMcpServer({ server: "s", label: "L" });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ server: "s", label: "L" });
  });
  it("removeMcpServer(id) calls DELETE /mcp/servers/{id}", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.removeMcpServer("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack");
    expect(opts.method).toBe("DELETE");
  });
  it("enableMcpServer(id) calls POST /mcp/servers/{id}/enable", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.enableMcpServer("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/enable");
    expect(opts.method).toBe("POST");
  });
  it("disableMcpServer(id) calls POST /mcp/servers/{id}/disable", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.disableMcpServer("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/disable");
    expect(opts.method).toBe("POST");
  });
  it("connectMcpServer(id) calls POST /mcp/servers/{id}/connect", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.connectMcpServer("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/connect");
    expect(opts.method).toBe("POST");
  });
  it("testMcpServer(id) calls POST /mcp/servers/{id}/test", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.testMcpServer("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/test");
    expect(opts.method).toBe("POST");
  });
  it("mcpTools(id) calls GET /mcp/servers/{id}/tools", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => [] });
    await d.mcpTools("slack");
    const [url] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/tools");
  });
  it("setMcpToolOverride(id, tool, patch) calls POST /mcp/servers/{id}/tools/{tool} with body", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.setMcpToolOverride("slack", "search", { enabled: false });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/tools/search");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ enabled: false });
  });
  it("mcpAuthStart(id) calls POST /mcp/servers/{id}/auth/start", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.mcpAuthStart("slack");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/mcp/servers/slack/auth/start");
    expect(opts.method).toBe("POST");
  });
  it("mcpSetToken(id, token) calls POST /secret with mcp.{id}.token name and value", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.mcpSetToken("slack", "tok");
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/secret");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ name: "mcp.slack.token", value: "tok" });
  });
});
