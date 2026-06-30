import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock daemon before importing orb.js
const daemonMock = {
  mcpServers: vi.fn().mockResolvedValue({
    servers: [
      { server: "slack", label: "Slack", icon: "💬", egress: "network" },
      { server: "files", label: "Files", icon: "📁", egress: "local" },
    ],
  }),
  on: vi.fn(),
  onOpen: vi.fn(),
  onClose: vi.fn(),
  connect: vi.fn(),
};

vi.mock("../lib/daemon.js", () => ({
  daemon: daemonMock,
}));

vi.mock("../lib/orb-renderer.js", () => ({
  createOrbRenderer: vi.fn(() => ({ state: "idle", start: vi.fn(), resize: vi.fn(), setState: vi.fn(), setAmplitude: vi.fn() })),
  SC: { idle: true, listening: true, thinking: true, talking: true },
}));

vi.mock("../lib/earcons.js", () => ({
  createEarcons: vi.fn(() => ({ resumeOnGesture: vi.fn(), playMode: vi.fn(), playState: vi.fn() })),
}));

vi.mock("../components/orb-cards/orb-cards.js", () => ({}));

// Set up the DOM before the module-level import so orb.js can find its elements.
document.body.innerHTML = `
  <div class="stage">
    <canvas id="gl"></canvas>
    <canvas id="ov"></canvas>
    <div id="conn">connecting…</div>
    <div id="net-ring" class="net-ring" aria-hidden="true"></div>
    <div id="net-conn" class="net-conn" aria-hidden="true">↗</div>
    <div id="net-cap" class="orbcap" aria-hidden="true"></div>
  </div>
  <orb-cards id="cards"></orb-cards>
  <div id="a11y-live" class="a11y-live" role="status" aria-live="assertive"></div>
`;

// Import orb.js once (module is cached; handlers are registered at import time).
await import("./orb.js");

// Wait for the onOpen callback to fire (it is registered synchronously in init())
// and for mcpServers() to resolve so _serverMap is populated.
const onOpenCb = daemonMock.onOpen.mock.calls[0]?.[0];
if (onOpenCb) await onOpenCb();
// Flush remaining microtasks so refreshServerMap()'s forEach has run.
await new Promise((r) => setTimeout(r, 0));

// Capture the step handler registered by orb.js — this is stable across all tests.
const stepCallback = daemonMock.on.mock.calls.find((c) => c[0] === "step")?.[1];

describe("Orb Egress Ring", () => {
  beforeEach(() => {
    // Reset only the ring elements between tests; the module state (_serverMap) persists.
    const ring = document.querySelector("#net-ring");
    const conn = document.querySelector("#net-conn");
    const cap = document.querySelector("#net-cap");
    if (ring) ring.classList.remove("active");
    if (conn) conn.classList.remove("active");
    if (cap) { cap.classList.remove("active"); cap.textContent = ""; }
  });

  it("activates ring and caption on network tool running", async () => {
    expect(stepCallback).toBeDefined();

    stepCallback({
      index: 0,
      label: "Send message",
      tool: "slack__send_message",
      status: "running",
    });

    await vi.waitFor(() => {
      const ring = document.querySelector("#net-ring");
      const conn = document.querySelector("#net-conn");
      const cap = document.querySelector("#net-cap");
      expect(ring.classList.contains("active")).toBe(true);
      expect(conn.classList.contains("active")).toBe(true);
      expect(cap.textContent).toBe("Reaching Slack…");
    });
  });

  it("deactivates ring on step done", async () => {
    expect(stepCallback).toBeDefined();

    // Activate first
    stepCallback({
      index: 0,
      label: "Send message",
      tool: "slack__send_message",
      status: "running",
    });

    await vi.waitFor(() => {
      expect(document.querySelector("#net-ring").classList.contains("active")).toBe(true);
    });

    // Now send done — active class must be removed (fade is CSS, not a JS delay)
    stepCallback({
      index: 0,
      label: "Send message",
      tool: "slack__send_message",
      status: "done",
    });

    await vi.waitFor(() => {
      const ring = document.querySelector("#net-ring");
      const conn = document.querySelector("#net-conn");
      expect(ring.classList.contains("active")).toBe(false);
      expect(conn.classList.contains("active")).toBe(false);
    });
  });

  it("does not activate ring for local server tool", async () => {
    expect(stepCallback).toBeDefined();

    stepCallback({
      index: 0,
      label: "Read file",
      tool: "files__read",
      status: "running",
    });

    // Wait 60ms (> any synchronous path) and verify ring stayed inactive.
    await new Promise((r) => setTimeout(r, 60));
    const ring = document.querySelector("#net-ring");
    expect(ring.classList.contains("active")).toBe(false);
  });

  it("does not activate ring for built-in tool (no separator)", async () => {
    expect(stepCallback).toBeDefined();

    stepCallback({
      index: 0,
      label: "Get time",
      tool: "get_time",
      status: "running",
    });

    // Wait 60ms and verify ring stayed inactive.
    await new Promise((r) => setTimeout(r, 60));
    const ring = document.querySelector("#net-ring");
    expect(ring.classList.contains("active")).toBe(false);
  });
});
