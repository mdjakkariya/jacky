import { describe, it, expect, vi, beforeEach } from "vitest";

// ---------------------------------------------------------------------------
// Mock daemon before importing the component under test
// ---------------------------------------------------------------------------
vi.mock("../../lib/daemon.js", () => ({
  daemon: {
    mcpTools: vi.fn().mockResolvedValue([
      { name: "search_messages", description: "Search across channels", risk: "read_only", network: false, enabled: true },
      { name: "send_message",    description: "Post a message",         risk: "write",     network: true,  enabled: true },
      { name: "delete_message",  description: "Delete a message",       risk: "destructive", network: true, enabled: false },
    ]),
    setMcpToolOverride: vi.fn().mockResolvedValue({ ok: true }),
    removeMcpServer:    vi.fn().mockResolvedValue({ ok: true }),
    connectMcpServer:   vi.fn().mockResolvedValue({ ok: true }),
    mcpAuthStart:       vi.fn().mockResolvedValue({ ok: false, message: "oauth not yet supported (phase 6)" }),
    mcpSetToken:        vi.fn().mockResolvedValue({ ok: true }),
    on: vi.fn().mockReturnValue(() => {}), // returns unsubscribe fn
  },
}));

import { daemon } from "../../lib/daemon.js";
import { showConnectionDetail, hideConnectionDetail } from "./connection-detail.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeContainer() {
  const el = document.createElement("div");
  document.body.appendChild(el);
  return el;
}

/** Mount the component and wait for the initial mcpTools() call to resolve. */
async function mount(container, id = "slack", meta = { label: "Slack", egress: true, state: "connected", auth_type: "oauth" }, opts = {}) {
  const onClose = opts.onClose || vi.fn();
  const el = showConnectionDetail(container, id, meta, { onClose });
  // Drain micro-task queue so the async mcpTools() load completes
  await new Promise((r) => setTimeout(r, 0));
  return { el, onClose };
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  // Reset mcpTools to the default three-tool list
  daemon.mcpTools.mockResolvedValue([
    { name: "search_messages", description: "Search across channels", risk: "read_only", network: false, enabled: true },
    { name: "send_message",    description: "Post a message",         risk: "write",     network: true,  enabled: true },
    { name: "delete_message",  description: "Delete a message",       risk: "destructive", network: true, enabled: false },
  ]);
});

// ---------------------------------------------------------------------------
// 1. Rendering
// ---------------------------------------------------------------------------
describe("Rendering", () => {
  it("renders a tool row for each tool returned by mcpTools()", async () => {
    const container = makeContainer();
    await mount(container);
    const rows = container.querySelectorAll(".tool-row");
    expect(rows.length).toBe(3);
  });

  it("shows tool name and description in each row", async () => {
    const container = makeContainer();
    await mount(container);
    const rows = container.querySelectorAll(".tool-row");
    const firstName  = rows[0].querySelector(".tool-name").textContent;
    const firstDesc  = rows[0].querySelector(".tool-desc").textContent;
    expect(firstName).toBe("search_messages");
    expect(firstDesc).toBe("Search across channels");
  });

  it("shows the correct risk pill class: read_only → read, write → write, destructive → danger", async () => {
    const container = makeContainer();
    await mount(container);
    const rows = container.querySelectorAll(".tool-row");
    expect(rows[0].querySelector(".pill").classList.contains("read")).toBe(true);
    expect(rows[1].querySelector(".pill").classList.contains("write")).toBe(true);
    expect(rows[2].querySelector(".pill").classList.contains("danger")).toBe(true);
  });

  it("shows a network badge on network tools", async () => {
    const container = makeContainer();
    await mount(container);
    const rows = container.querySelectorAll(".tool-row");
    // Row 0 is local — no net badge; rows 1 & 2 are network — net badge present
    expect(rows[0].querySelector(".pill.net")).toBeNull();
    expect(rows[1].querySelector(".pill.net")).not.toBeNull();
    expect(rows[2].querySelector(".pill.net")).not.toBeNull();
  });

  it("enable toggle is checked for enabled tools and unchecked for disabled tools", async () => {
    const container = makeContainer();
    await mount(container);
    const rows = container.querySelectorAll(".tool-row");
    const toggle0 = rows[0].querySelector("input[type='checkbox']");
    const toggle2 = rows[2].querySelector("input[type='checkbox']");
    expect(toggle0.checked).toBe(true);   // enabled: true
    expect(toggle2.checked).toBe(false);  // enabled: false
  });

  it("renders network egress banner when meta.egress is truthy", async () => {
    const container = makeContainer();
    await mount(container, "slack", { label: "Slack", egress: true, state: "connected", auth_type: "oauth" });
    // Should show an orange/network banner (not .local)
    const banner = container.querySelector(".mcp-banner");
    expect(banner).not.toBeNull();
    expect(banner.classList.contains("local")).toBe(false);
  });

  it("renders local banner when meta.egress is falsy", async () => {
    const container = makeContainer();
    daemon.mcpTools.mockResolvedValue([]);
    await mount(container, "files", { label: "Files", egress: false, state: "connected", auth_type: "none" });
    const banner = container.querySelector(".mcp-banner");
    expect(banner).not.toBeNull();
    expect(banner.classList.contains("local")).toBe(true);
  });

  it("shows the tools count in the section label", async () => {
    const container = makeContainer();
    await mount(container);
    // Section label contains "Tools · 3"
    const sectionText = container.querySelector(".detail-tools-label")?.textContent || "";
    expect(sectionText).toContain("3");
  });
});

// ---------------------------------------------------------------------------
// 2. Enable toggle
// ---------------------------------------------------------------------------
describe("Enable toggle", () => {
  it("toggling off an enabled tool calls setMcpToolOverride(id, tool, {enabled:false})", async () => {
    const container = makeContainer();
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    // Row 0: search_messages — enabled:true
    const toggle = rows[0].querySelector("input[type='checkbox']");
    toggle.checked = false;
    toggle.dispatchEvent(new Event("change"));
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "search_messages", { enabled: false });
  });

  it("toggling on a disabled tool calls setMcpToolOverride(id, tool, {enabled:true})", async () => {
    const container = makeContainer();
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    // Row 2: delete_message — enabled:false
    const toggle = rows[2].querySelector("input[type='checkbox']");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change"));
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "delete_message", { enabled: true });
  });
});

// ---------------------------------------------------------------------------
// 3. Risk pill cycle
// ---------------------------------------------------------------------------
describe("Risk pill cycle (local tool)", () => {
  it("clicking the pill on a read_only tool advances to write", async () => {
    const container = makeContainer();
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    // Row 0: search_messages — read_only, not network
    const pill = rows[0].querySelector(".pill:not(.net)");
    pill.click();
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "search_messages", { risk: "write" });
  });

  it("clicking the pill on a write tool advances to destructive", async () => {
    const container = makeContainer();
    // Provide a local write tool so there's no network floor
    daemon.mcpTools.mockResolvedValue([
      { name: "local_write", description: "Write a file", risk: "write", network: false, enabled: true },
    ]);
    await mount(container, "files");
    const rows = container.querySelectorAll(".tool-row");
    const pill = rows[0].querySelector(".pill:not(.net)");
    pill.click();
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("files", "local_write", { risk: "destructive" });
  });

  it("clicking the pill on a destructive tool wraps back to read_only", async () => {
    const container = makeContainer();
    daemon.mcpTools.mockResolvedValue([
      { name: "local_danger", description: "Danger op", risk: "destructive", network: false, enabled: true },
    ]);
    await mount(container, "files");
    const rows = container.querySelectorAll(".tool-row");
    const pill = rows[0].querySelector(".pill:not(.net)");
    pill.click();
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("files", "local_danger", { risk: "read_only" });
  });
});

// ---------------------------------------------------------------------------
// 4. Network floor — network tools cannot go below write
// ---------------------------------------------------------------------------
describe("Network floor (network tools cannot go below write)", () => {
  it("clicking a network tool whose risk is write does NOT advance to read_only but to destructive", async () => {
    const container = makeContainer();
    // send_message: risk=write, network=true
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    // Row 1 is send_message (write, network:true)
    const pill = rows[1].querySelector(".pill:not(.net)");
    pill.click();
    // write → should go to destructive (not read_only, because network floor applies)
    // The floor means: next after write for a network tool is destructive (normal progression)
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "send_message", { risk: "destructive" });
  });

  it("clicking a network tool whose risk is read_only advances to write (floor prevents staying at read_only)", async () => {
    const container = makeContainer();
    daemon.mcpTools.mockResolvedValue([
      { name: "net_read", description: "Network read op", risk: "read_only", network: true, enabled: true },
    ]);
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    const pill = rows[0].querySelector(".pill:not(.net)");
    pill.click();
    // read_only → next is write (normal), but even if we were going to read_only from destructive,
    // the floor would bump it to write. Here read_only → write.
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "net_read", { risk: "write" });
  });

  it("cycling a network tool from destructive wraps to write (not read_only) due to floor", async () => {
    const container = makeContainer();
    daemon.mcpTools.mockResolvedValue([
      { name: "net_danger", description: "Network danger op", risk: "destructive", network: true, enabled: true },
    ]);
    await mount(container, "slack");
    const rows = container.querySelectorAll(".tool-row");
    const pill = rows[0].querySelector(".pill:not(.net)");
    pill.click();
    // destructive → next in normal cycle is read_only, but floor bumps it to write
    expect(daemon.setMcpToolOverride).toHaveBeenCalledWith("slack", "net_danger", { risk: "write" });
  });
});

// ---------------------------------------------------------------------------
// 5. Danger zone actions
// ---------------------------------------------------------------------------
describe("Danger zone", () => {
  it("Remove connection calls removeMcpServer(id) then onClose", async () => {
    const container = makeContainer();
    const onClose = vi.fn();
    await mount(container, "slack", { label: "Slack", egress: true, state: "connected", auth_type: "oauth" }, { onClose });
    const removeBtn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent.includes("Remove")
    );
    expect(removeBtn).not.toBeUndefined();
    removeBtn.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(daemon.removeMcpServer).toHaveBeenCalledWith("slack");
    expect(onClose).toHaveBeenCalled();
    // onClose is called after removeMcpServer
    expect(daemon.removeMcpServer.mock.invocationCallOrder[0]).toBeLessThan(
      onClose.mock.invocationCallOrder[0]
    );
  });

  it("Re-sync tools calls connectMcpServer(id) then reloads tool list", async () => {
    const container = makeContainer();
    await mount(container, "slack");
    const resyncBtn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent.includes("Re-sync") || b.textContent.includes("sync")
    );
    expect(resyncBtn).not.toBeUndefined();

    const callsBefore = daemon.mcpTools.mock.calls.length; // should be 1 (initial load)
    resyncBtn.click();
    await new Promise((r) => setTimeout(r, 0));

    expect(daemon.connectMcpServer).toHaveBeenCalledWith("slack");
    // mcpTools should be called again (reload)
    expect(daemon.mcpTools.mock.calls.length).toBeGreaterThan(callsBefore);
  });
});

// ---------------------------------------------------------------------------
// 6. mcp_status subscription — debounce + status dot update
// ---------------------------------------------------------------------------
describe("mcp_status subscription", () => {
  it("registers a listener via daemon.on('mcp_status', ...) on mount", async () => {
    const container = makeContainer();
    await mount(container, "slack");
    // daemon.on should have been called with "mcp_status"
    const calls = daemon.on.mock.calls;
    expect(calls.some((c) => c[0] === "mcp_status")).toBe(true);
  });

  it("mcp_status event for this server immediately shows reconnecting status dot", async () => {
    let capturedHandler = null;
    daemon.on.mockImplementation((type, fn) => {
      if (type === "mcp_status") capturedHandler = fn;
      return () => {};
    });

    const container = makeContainer();
    await mount(container, "slack");

    expect(capturedHandler).not.toBeNull();

    // Fire an mcp_status event for this server
    capturedHandler({ server: "slack", state: "connected", tool_count: 3 });

    // The status dot should immediately show 'reconnecting'
    const dot = container.querySelector(".status-dot");
    expect(dot).not.toBeNull();
    expect(dot.classList.contains("reconnecting")).toBe(true);
  });

  it("mcp_status event for a DIFFERENT server does not change the header", async () => {
    let capturedHandler = null;
    daemon.on.mockImplementation((type, fn) => {
      if (type === "mcp_status") capturedHandler = fn;
      return () => {};
    });

    const container = makeContainer();
    await mount(container, "slack");

    // The initial state dot should be "connected"
    const dotBefore = container.querySelector(".status-dot");
    const classBefore = dotBefore ? dotBefore.className : "";

    // Fire for a different server
    capturedHandler({ server: "github", state: "disconnected", tool_count: 0 });

    const dotAfter = container.querySelector(".status-dot");
    expect(dotAfter.className).toBe(classBefore); // unchanged
  });
});

// ---------------------------------------------------------------------------
// 7. hideConnectionDetail
// ---------------------------------------------------------------------------
describe("hideConnectionDetail", () => {
  it("removes the detail card from the container", async () => {
    const container = makeContainer();
    await mount(container);
    expect(container.querySelector(".connection-detail-card")).not.toBeNull();
    hideConnectionDetail(container);
    expect(container.querySelector(".connection-detail-card")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 8. Sign out button
// ---------------------------------------------------------------------------
describe("Sign out button", () => {
  it("Sign out button is rendered", async () => {
    const container = makeContainer();
    await mount(container, "slack", { label: "Slack", egress: true, state: "connected", auth_type: "token" });
    const signOutBtn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent.includes("Sign out")
    );
    expect(signOutBtn).not.toBeUndefined();
  });

  it("Sign out button for token server calls mcpSetToken(id, '') to clear credential", async () => {
    const container = makeContainer();
    await mount(container, "slack", { label: "Slack", egress: true, state: "connected", auth_type: "token" });
    const signOutBtn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent.includes("Sign out")
    );
    signOutBtn.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(daemon.mcpSetToken).toHaveBeenCalledWith("slack", "");
  });

  it("Sign out button for oauth server shows coming-soon note", async () => {
    const container = makeContainer();
    await mount(container, "slack", { label: "Slack", egress: true, state: "connected", auth_type: "oauth" });
    const signOutBtn = [...container.querySelectorAll("button")].find(
      (b) => b.textContent.includes("Sign out")
    );
    signOutBtn.click();
    await new Promise((r) => setTimeout(r, 0));
    // Should call mcpAuthStart or show a note — check for "coming" text in the card
    const cardText = container.textContent.toLowerCase();
    expect(
      daemon.mcpAuthStart.mock.calls.length > 0 || cardText.includes("coming")
    ).toBe(true);
  });
});
