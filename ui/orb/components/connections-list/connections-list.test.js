import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: {
    mcpServers: vi.fn(),
    enableMcpServer: vi.fn().mockResolvedValue({ ok: true }),
    disableMcpServer: vi.fn().mockResolvedValue({ ok: true }),
    on: vi.fn().mockReturnValue(() => {}),
  },
}));
import { daemon } from "../../lib/daemon.js";
import "./connections-list.js";

const NET_SRV = {
  server: "slack",
  label: "Slack",
  enabled: true,
  egress: "network",
  auth_type: "OAuth",
  state: "connected",
  tool_count: 7,
  secret_present: true,
  icon: "💬",
};

const LOCAL_SRV = {
  server: "localfiles",
  label: "Local Files",
  enabled: true,
  egress: "local",
  auth_type: "stdio",
  state: "connected",
  tool_count: 4,
  secret_present: true,
  icon: "📁",
};

const AUTH_NEEDED_SRV = {
  server: "github",
  label: "GitHub",
  enabled: false,
  egress: "network",
  auth_type: "OAuth",
  state: "auth_needed",
  tool_count: 0,
  secret_present: false,
  icon: "🐙",
};

beforeEach(() => {
  vi.clearAllMocks();
  daemon.on.mockReturnValue(() => {});
  document.body.innerHTML = '<connections-list id="connList"></connections-list>';
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ConnectionsList — rendering", () => {
  it("renders one card per server from mcpServers()", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV, LOCAL_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    expect(list.querySelectorAll(".srv-card").length).toBe(2);
  });

  it("renders server label in each card", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    expect(list.querySelector(".srv-name").textContent).toContain("Slack");
  });

  it("shows .pill.net badge and host when egress === 'network'", async () => {
    // Provide a url so egressHost() can extract a real hostname.
    const srvWithUrl = { ...NET_SRV, url: "https://api.slack.com" };
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [srvWithUrl] });
    const list = document.getElementById("connList");
    await list.load();
    const pill = list.querySelector(".pill.net");
    expect(pill).not.toBeNull();
    expect(pill.textContent).toContain("api.slack.com");
  });

  it("shows .pill.net badge using server id fallback when no url", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const pill = list.querySelector(".pill.net");
    expect(pill).not.toBeNull();
    // Falls back to bare server id, not server.com
    expect(pill.textContent).toContain("slack");
    expect(pill.textContent).not.toContain(".com");
  });

  it("shows .pill.local badge when egress === 'local' (not .pill.net)", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [LOCAL_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    // egress="local" must render the on-device pill, not the network pill
    const localPill = list.querySelector(".pill.local");
    const netPill = list.querySelector(".pill.net");
    expect(localPill).not.toBeNull();
    expect(localPill.textContent).toContain("on-device");
    expect(netPill).toBeNull();
  });

  it("status dot has class 'connected' when state === 'connected'", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const dot = list.querySelector(".status-dot");
    expect(dot.classList.contains("connected")).toBe(true);
  });

  it("status dot has class 'auth_needed' when state === 'auth_needed'", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [AUTH_NEEDED_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const dot = list.querySelector(".status-dot");
    expect(dot.classList.contains("auth_needed")).toBe(true);
  });

  it("shows 'Sign-in needed' description when state === 'auth_needed'", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [AUTH_NEEDED_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    expect(list.querySelector(".srv-desc").textContent).toContain("Sign-in needed");
  });

  it("shows 'Connected · N tools · auth_type' when state === 'connected'", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const desc = list.querySelector(".srv-desc").textContent;
    expect(desc).toContain("Connected");
    expect(desc).toContain("7");
    expect(desc).toContain("OAuth");
  });

  it("toggle checkbox is checked when server is enabled", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const checkbox = list.querySelector("input[type='checkbox']");
    expect(checkbox.checked).toBe(true);
  });

  it("toggle checkbox is unchecked when server is disabled", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [AUTH_NEEDED_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const checkbox = list.querySelector("input[type='checkbox']");
    expect(checkbox.checked).toBe(false);
  });
});

describe("ConnectionsList — toggle interactions", () => {
  it("toggle click calls disableMcpServer(id) when currently enabled", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const checkbox = list.querySelector("input[type='checkbox']");
    checkbox.click();
    expect(daemon.disableMcpServer).toHaveBeenCalledWith("slack");
    expect(daemon.enableMcpServer).not.toHaveBeenCalled();
  });

  it("toggle click calls enableMcpServer(id) when currently disabled", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [AUTH_NEEDED_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const checkbox = list.querySelector("input[type='checkbox']");
    checkbox.click();
    expect(daemon.enableMcpServer).toHaveBeenCalledWith("github");
    expect(daemon.disableMcpServer).not.toHaveBeenCalled();
  });
});

describe("ConnectionsList — events", () => {
  it("clicking the card body (not toggle) dispatches 'server-select' with full server row", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const list = document.getElementById("connList");
    await list.load();
    const card = list.querySelector(".srv-card");
    let received = null;
    list.addEventListener("server-select", (e) => { received = e.detail; });
    card.querySelector(".srv-meta").click();
    // detail is now the full server row, not just the id
    expect(received).toEqual(expect.objectContaining({ server: "slack", label: "Slack", egress: "network" }));
  });

  it("'+ Add connection' button dispatches 'add-connection' event", async () => {
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [] });
    const list = document.getElementById("connList");
    await list.load();
    let fired = false;
    list.addEventListener("add-connection", () => { fired = true; });
    list.querySelector(".add-conn-btn").click();
    expect(fired).toBe(true);
  });
});

describe("ConnectionsList — load() guard", () => {
  it("concurrent load() calls do not stack rows", async () => {
    let resolve;
    const pending = new Promise((r) => { resolve = r; });
    daemon.mcpServers.mockReturnValue(pending);
    const list = document.getElementById("connList");
    // Start two loads simultaneously; only one should actually render
    const p1 = list.load();
    const p2 = list.load(); // should be a no-op
    resolve({ ok: true, servers: [NET_SRV] });
    await Promise.all([p1, p2]);
    expect(list.querySelectorAll(".srv-card").length).toBe(1);
    expect(daemon.mcpServers).toHaveBeenCalledTimes(1);
  });
});

describe("ConnectionsList — reconnect-churn debounce", () => {
  /** Helper: create a fresh element AFTER wiring daemon.on so connectedCallback
   *  picks up the spy that captures the handler. Returns {list, capturedHandler}. */
  function makeListWithHandlerCapture() {
    let capturedHandler = null;
    daemon.on.mockImplementation((type, fn) => {
      if (type === "mcp_status") capturedHandler = fn;
      return () => {};
    });
    // Remove the element inserted by beforeEach and create a fresh one so
    // connectedCallback fires against our newly wired mock.
    document.body.innerHTML = "";
    const list = document.createElement("connections-list");
    document.body.appendChild(list);
    return { list, get capturedHandler() { return capturedHandler; } };
  }

  it("rapid disconnect→connect within window does NOT show off dot; shows 'reconnecting…'", async () => {
    vi.useFakeTimers();
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const ctx = makeListWithHandlerCapture();
    const { list } = ctx;
    await list.load();

    // Simulate disconnect event
    ctx.capturedHandler({ type: "mcp_status", server: "slack", state: "disconnected", tool_count: 0 });

    // During debounce window: should show "reconnecting…", not the off dot
    const dot = list.querySelector(".status-dot");
    expect(dot.classList.contains("reconnecting")).toBe(true);
    expect(list.querySelector(".srv-desc").textContent).toContain("reconnecting");
    // Should NOT show the off/disconnected dot
    expect(dot.classList.contains("disconnected")).toBe(false);
    expect(dot.classList.contains("connected")).toBe(false);

    // Simulate reconnect within window (before 1500ms)
    ctx.capturedHandler({ type: "mcp_status", server: "slack", state: "connected", tool_count: 7 });

    // Still in window — should still be "reconnecting…"
    expect(dot.classList.contains("reconnecting")).toBe(true);

    // Advance past debounce window
    vi.advanceTimersByTime(1500);

    // Now should settle to connected
    expect(dot.classList.contains("connected")).toBe(true);
    expect(dot.classList.contains("reconnecting")).toBe(false);
    expect(list.querySelector(".srv-desc").textContent).not.toContain("reconnecting");
    // auth_type must survive the reconnect cycle (it's stashed on the card, not scraped
    // from the description text which was overwritten with "reconnecting…").
    expect(list.querySelector(".srv-desc").textContent).toContain("OAuth");
  });

  it("disconnect NOT followed by reconnect within window settles to off/error dot", async () => {
    vi.useFakeTimers();
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV] });
    const ctx = makeListWithHandlerCapture();
    const { list } = ctx;
    await list.load();

    // Simulate disconnect event
    ctx.capturedHandler({ type: "mcp_status", server: "slack", state: "disconnected", tool_count: 0 });

    // During window: reconnecting
    const dot = list.querySelector(".status-dot");
    expect(dot.classList.contains("reconnecting")).toBe(true);

    // Advance past debounce window with no reconnect
    vi.advanceTimersByTime(1500);

    // Should now show the disconnected/error dot
    expect(dot.classList.contains("disconnected")).toBe(true);
    expect(dot.classList.contains("reconnecting")).toBe(false);
    expect(dot.classList.contains("connected")).toBe(false);
  });

  it("mcp_status event for a different server does not affect other cards", async () => {
    vi.useFakeTimers();
    daemon.mcpServers.mockResolvedValue({ ok: true, servers: [NET_SRV, LOCAL_SRV] });
    const ctx = makeListWithHandlerCapture();
    const { list } = ctx;
    await list.load();

    const cards = list.querySelectorAll(".srv-card");
    const slackCard = cards[0];
    const localCard = cards[1];

    // Only slack gets disconnected
    ctx.capturedHandler({ type: "mcp_status", server: "slack", state: "disconnected", tool_count: 0 });

    // Slack should be reconnecting
    expect(slackCard.querySelector(".status-dot").classList.contains("reconnecting")).toBe(true);
    // Local should still be connected
    expect(localCard.querySelector(".status-dot").classList.contains("connected")).toBe(true);
  });
});

describe("ConnectionsList — WS subscription lifecycle", () => {
  it("subscribes to mcp_status in connectedCallback", () => {
    // daemon.on is called during connectedCallback (element already in DOM from beforeEach)
    expect(daemon.on).toHaveBeenCalledWith("mcp_status", expect.any(Function));
  });

  it("unsubscribes in disconnectedCallback", () => {
    const unsubSpy = vi.fn();
    daemon.on.mockReturnValue(unsubSpy);
    // Re-insert element to trigger a fresh connectedCallback
    document.body.innerHTML = "";
    const el = document.createElement("connections-list");
    document.body.appendChild(el);
    // Remove to trigger disconnectedCallback
    el.remove();
    expect(unsubSpy).toHaveBeenCalled();
  });
});
