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

describe("Orb Egress Ring", () => {
  beforeEach(() => {
    // Clear the DOM
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
    vi.clearAllMocks();
  });

  it("activates ring and caption on network tool running", (done) => {
    // Import after mocks are set up
    import("./orb.js").then(() => {
      // Capture the daemon.on callback
      const calls = daemonMock.on.mock.calls;
      const stepCallback = calls.find((c) => c[0] === "step")?.[1];
      expect(stepCallback).toBeDefined();

      // Wait for server map to load
      setTimeout(() => {
        stepCallback({
          index: 0,
          label: "Send message",
          tool: "slack__send_message",
          status: "running",
        });

        const ring = document.querySelector("#net-ring");
        const conn = document.querySelector("#net-conn");
        const cap = document.querySelector("#net-cap");

        expect(ring.classList.contains("active")).toBe(true);
        expect(conn.classList.contains("active")).toBe(true);
        expect(cap.textContent).toBe("Reaching Slack…");
        done();
      }, 50);
    });
  });

  it("deactivates ring on step done", (done) => {
    import("./orb.js").then(() => {
      const calls = daemonMock.on.mock.calls;
      const stepCallback = calls.find((c) => c[0] === "step")?.[1];
      expect(stepCallback).toBeDefined();

      setTimeout(() => {
        // Activate
        stepCallback({
          index: 0,
          label: "Send message",
          tool: "slack__send_message",
          status: "running",
        });

        const ring = document.querySelector("#net-ring");
        const conn = document.querySelector("#net-conn");
        expect(ring.classList.contains("active")).toBe(true);

        // Deactivate
        stepCallback({
          index: 0,
          label: "Send message",
          tool: "slack__send_message",
          status: "done",
        });

        // Remove active class immediately; fade is CSS
        expect(ring.classList.contains("active")).toBe(false);
        expect(conn.classList.contains("active")).toBe(false);
        done();
      }, 50);
    });
  });

  it("does not activate ring for local server tool", (done) => {
    import("./orb.js").then(() => {
      const calls = daemonMock.on.mock.calls;
      const stepCallback = calls.find((c) => c[0] === "step")?.[1];
      expect(stepCallback).toBeDefined();

      setTimeout(() => {
        stepCallback({
          index: 0,
          label: "Read file",
          tool: "files__read",
          status: "running",
        });

        const ring = document.querySelector("#net-ring");
        expect(ring.classList.contains("active")).toBe(false);
        done();
      }, 50);
    });
  });

  it("does not activate ring for built-in tool (no separator)", (done) => {
    import("./orb.js").then(() => {
      const calls = daemonMock.on.mock.calls;
      const stepCallback = calls.find((c) => c[0] === "step")?.[1];
      expect(stepCallback).toBeDefined();

      setTimeout(() => {
        stepCallback({
          index: 0,
          label: "Get time",
          tool: "get_time",
          status: "running",
        });

        const ring = document.querySelector("#net-ring");
        expect(ring.classList.contains("active")).toBe(false);
        done();
      }, 50);
    });
  });
});
