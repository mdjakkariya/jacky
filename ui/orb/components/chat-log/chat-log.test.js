import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/tauri.js", () => ({ openExternal: vi.fn() }));
vi.mock("../../lib/daemon.js", () => ({
  daemon: {
    mcpServers: vi.fn().mockResolvedValue({ servers: [] }),
  },
}));
import "./chat-log.js";
import { mcpInfoForTool } from "./chat-log.js";

function mount() {
  document.body.innerHTML = '<chat-log id="log"><div id="empty">welcome</div></chat-log><button id="jump" class="hidden"></button>';
  return document.querySelector("chat-log");
}

beforeEach(() => { vi.clearAllMocks(); });

it("renders a markdown bubble for Jack and escapes/strong-wraps", () => {
  const log = mount();
  const d = log.bubble("jack", "**hi**", true);
  expect(d.className).toBe("msg jack");
  expect(d.innerHTML).toContain("<strong>hi</strong>");
});

it("renders a plain-text bubble for the user (no HTML interpretation)", () => {
  const log = mount();
  const d = log.bubble("me", "<b>x</b>", false);
  expect(d.textContent).toBe("<b>x</b>");
  expect(d.querySelector("b")).toBeNull();
});

it("removes the welcome block on first bubble", () => {
  const log = mount();
  expect(log.querySelector("#empty")).not.toBeNull();
  log.bubble("me", "hi");
  expect(log.querySelector("#empty")).toBeNull();
});

it("keeps only one typing indicator", () => {
  const log = mount();
  log.showTyping(); log.showTyping();
  expect(log.querySelectorAll(".typing").length).toBe(1);
  log.hideTyping();
  expect(log.querySelectorAll(".typing").length).toBe(0);
});

it("updates a step row in place by index", () => {
  const log = mount();
  log.renderStep({ index: 0, label: "Search", status: "running" });
  log.renderStep({ index: 0, label: "Search", status: "done" });
  const rows = log.querySelectorAll(".steptrace [data-i='0']");
  expect(rows.length).toBe(1);
  expect(rows[0].className).toBe("row done");
});

it("chips emit chip-send with their data-send text", () => {
  const log = mount();
  log.innerHTML = '<button class="chip" data-send="open github">x</button>';
  log._bindChips(log);
  const spy = vi.fn();
  log.addEventListener("chip-send", (e) => spy(e.detail));
  log.querySelector(".chip").click();
  expect(spy).toHaveBeenCalledWith("open github");
});

// --- MCP info and step badges --------------------------------------------------
describe("mcpInfoForTool", () => {
  it("returns null for undefined/null tool", () => {
    const serverMap = { slack: { label: "Slack", icon: "💬", egress: "network" } };
    expect(mcpInfoForTool(undefined, serverMap)).toBeNull();
    expect(mcpInfoForTool(null, serverMap)).toBeNull();
  });

  it("returns null for tools without __ separator (built-in)", () => {
    const serverMap = { slack: { label: "Slack", icon: "💬", egress: "network" } };
    expect(mcpInfoForTool("get_time", serverMap)).toBeNull();
  });

  it("returns null for unknown server prefix", () => {
    const serverMap = { slack: { label: "Slack", icon: "💬", egress: "network" } };
    expect(mcpInfoForTool("unknown__x", serverMap)).toBeNull();
  });

  it("returns MCP info for network tool", () => {
    const serverMap = { slack: { label: "Slack", icon: "💬", egress: "network" } };
    const info = mcpInfoForTool("slack__search_messages", serverMap);
    expect(info).toEqual({
      id: "slack",
      label: "Slack",
      icon: "💬",
      egress: true,
      shortName: "search_messages",
    });
  });

  it("returns MCP info with egress:false for local server", () => {
    const serverMap = { files: { label: "Files", icon: "📁", egress: "local" } };
    const info = mcpInfoForTool("files__read", serverMap);
    expect(info).toEqual({
      id: "files",
      label: "Files",
      icon: "📁",
      egress: false,
      shortName: "read",
    });
  });

  it("defaults icon to 🔌 when missing", () => {
    const serverMap = { custom: { label: "Custom", egress: "network" } };
    const info = mcpInfoForTool("custom__action", serverMap);
    expect(info.icon).toBe("🔌");
  });

  it("returns null for empty prefix", () => {
    const serverMap = { slack: { label: "Slack", icon: "💬", egress: "network" } };
    expect(mcpInfoForTool("__search", serverMap)).toBeNull();
  });
});

describe("renderStep integration (no server map)", () => {
  it("renders step without badge when server map is empty (no MCP)", () => {
    const log = mount();
    log.renderStep({ index: 0, tool: "get_time", label: "Get time", status: "running" });
    const row = log.querySelector('.steptrace [data-i="0"]');
    expect(row).not.toBeNull();
    expect(row.querySelector(".label").textContent).toContain("Get time…");
    expect(row.querySelector(".srvbadge")).toBeNull();
  });

  it("can update a step from running to done without badge", () => {
    const log = mount();
    log.renderStep({ index: 0, tool: "get_time", label: "Get time", status: "running" });
    log.renderStep({ index: 0, tool: "get_time", label: "Get time", status: "done" });
    const row = log.querySelector('.steptrace [data-i="0"]');
    expect(row.className).toBe("row done");
    expect(row.querySelector(".label").textContent).toContain("✓");
  });
});
