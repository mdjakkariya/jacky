/** Tests for the Privacy summary inventory logic (privacyExits) and DOM renderer
 *  (renderPrivacySummary). Pure-logic tests run without any DOM setup; DOM tests
 *  use happy-dom (vitest default for this project). */
import { describe, it, expect, vi } from "vitest";
import { privacyExits, renderPrivacySummary } from "./privacy-summary.js";

// ---------------------------------------------------------------------------
// privacyExits — pure logic, no DOM needed
// ---------------------------------------------------------------------------

const NO_EXITS_SETTINGS = { llm_provider: "ollama", allow_web: false };
const NET_SERVER = { server: "slack", label: "Slack", enabled: true, egress: "network", icon: "💬", url: "https://slack.com" };
const LOCAL_SERVER = { server: "localfiles", label: "Local Files", enabled: true, egress: null, icon: "📁" };
const DISABLED_NET_SERVER = { server: "github", label: "GitHub", enabled: false, egress: "network", icon: "🐙", url: "https://github.com" };

describe("privacyExits", () => {
  it("returns empty list when nothing is enabled", () => {
    expect(privacyExits(NO_EXITS_SETTINGS, [])).toEqual([]);
  });

  it("includes web search when allow_web is true", () => {
    const exits = privacyExits({ ...NO_EXITS_SETTINGS, allow_web: true }, []);
    expect(exits).toHaveLength(1);
    expect(exits[0].name).toBe("Web search");
    expect(exits[0].icon).toBe("🔎");
  });

  it("does NOT include web search when allow_web is false", () => {
    const exits = privacyExits({ ...NO_EXITS_SETTINGS, allow_web: false }, []);
    expect(exits.find((e) => e.name === "Web search")).toBeUndefined();
  });

  it("includes cloud LLM when llm_provider is anthropic", () => {
    const exits = privacyExits({ llm_provider: "anthropic", allow_web: false }, []);
    expect(exits).toHaveLength(1);
    expect(exits[0].name).toBe("Cloud LLM (Anthropic)");
    expect(exits[0].icon).toBe("🧠");
  });

  it("does NOT include cloud LLM when llm_provider is ollama", () => {
    const exits = privacyExits({ llm_provider: "ollama", allow_web: false }, []);
    expect(exits.find((e) => e.name === "Cloud LLM (Anthropic)")).toBeUndefined();
  });

  it("includes enabled network-egress MCP servers", () => {
    const exits = privacyExits(NO_EXITS_SETTINGS, [NET_SERVER]);
    expect(exits).toHaveLength(1);
    expect(exits[0].name).toBe("Slack");
    expect(exits[0].icon).toBe("💬");
    expect(exits[0].desc).toContain("slack.com");
  });

  it("does NOT include disabled network-egress MCP servers", () => {
    const exits = privacyExits(NO_EXITS_SETTINGS, [DISABLED_NET_SERVER]);
    expect(exits).toHaveLength(0);
  });

  it("does NOT include enabled local (non-egress) MCP servers", () => {
    const exits = privacyExits(NO_EXITS_SETTINGS, [LOCAL_SERVER]);
    expect(exits).toHaveLength(0);
  });

  it("lists all three active exits when all are on", () => {
    const settings = { llm_provider: "anthropic", allow_web: true };
    const exits = privacyExits(settings, [NET_SERVER, LOCAL_SERVER, DISABLED_NET_SERVER]);
    expect(exits).toHaveLength(3);
    const names = exits.map((e) => e.name);
    expect(names).toContain("Web search");
    expect(names).toContain("Cloud LLM (Anthropic)");
    expect(names).toContain("Slack");
    // Local and disabled servers must NOT appear
    expect(names).not.toContain("Local Files");
    expect(names).not.toContain("GitHub");
  });

  it("orders exits: web, cloud LLM, then MCP servers", () => {
    const settings = { llm_provider: "anthropic", allow_web: true };
    const exits = privacyExits(settings, [NET_SERVER]);
    expect(exits[0].name).toBe("Web search");
    expect(exits[1].name).toBe("Cloud LLM (Anthropic)");
    expect(exits[2].name).toBe("Slack");
  });

  it("uses server id as fallback hostname when url is absent", () => {
    const noUrl = { server: "myservice", label: "My Service", enabled: true, egress: "network", icon: "🔌" };
    const exits = privacyExits(NO_EXITS_SETTINGS, [noUrl]);
    expect(exits[0].desc).toContain("myservice");
  });

  it("handles multiple network MCP servers", () => {
    const second = { server: "github", label: "GitHub", enabled: true, egress: "network", icon: "🐙", url: "https://api.github.com" };
    const exits = privacyExits(NO_EXITS_SETTINGS, [NET_SERVER, second]);
    expect(exits).toHaveLength(2);
    const names = exits.map((e) => e.name);
    expect(names).toContain("Slack");
    expect(names).toContain("GitHub");
  });
});

// ---------------------------------------------------------------------------
// renderPrivacySummary — DOM integration (happy-dom)
// CSS tokens used: .mcp-banner.local, .exit-row, .exit-icon, .exit-meta,
//                  .exit-name, .exit-desc, button.btn.ghost-sm
// ---------------------------------------------------------------------------

describe("renderPrivacySummary", () => {
  function makeContainer() {
    const el = document.createElement("div");
    document.body.appendChild(el);
    return el;
  }

  it("renders a section heading with the exit count", () => {
    const el = makeContainer();
    const exits = [{ icon: "🔎", name: "Web search", desc: "Sends only your search query" }];
    renderPrivacySummary(el, exits, vi.fn());
    const head = el.querySelector(".label");
    expect(head).not.toBeNull();
    expect(head.textContent).toContain("1");
  });

  it("renders one .exit-row per exit", () => {
    const el = makeContainer();
    const exits = [
      { icon: "🔎", name: "Web search", desc: "Sends only your search query" },
      { icon: "🧠", name: "Cloud LLM (Anthropic)", desc: "Sends conversation + memory profile + tool results" },
    ];
    renderPrivacySummary(el, exits, vi.fn());
    expect(el.querySelectorAll(".exit-row")).toHaveLength(2);
  });

  it("renders exit icon (.exit-icon), name (.exit-name), and description (.exit-desc)", () => {
    const el = makeContainer();
    const exits = [{ icon: "💬", name: "Slack", desc: "Sends data to slack.com" }];
    renderPrivacySummary(el, exits, vi.fn());
    expect(el.querySelector(".exit-icon").textContent).toBe("💬");
    expect(el.querySelector(".exit-name").textContent).toBe("Slack");
    expect(el.querySelector(".exit-desc").textContent).toBe("Sends data to slack.com");
  });

  it("shows a no-exits message when the list is empty", () => {
    const el = makeContainer();
    renderPrivacySummary(el, [], vi.fn());
    expect(el.querySelectorAll(".exit-row")).toHaveLength(0);
    expect(el.textContent).toContain("No off-device exits");
  });

  it("renders the 'View audit log' button and calls the callback on click", () => {
    const el = makeContainer();
    const onAudit = vi.fn();
    renderPrivacySummary(el, [], onAudit);
    const btn = el.querySelector("button.btn");
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("View audit log");
    btn.click();
    expect(onAudit).toHaveBeenCalledOnce();
  });

  it("re-renders cleanly when called twice (no duplicate rows)", () => {
    const el = makeContainer();
    const exits = [{ icon: "🔎", name: "Web search", desc: "query only" }];
    renderPrivacySummary(el, exits, vi.fn());
    renderPrivacySummary(el, exits, vi.fn());
    expect(el.querySelectorAll(".exit-row")).toHaveLength(1);
  });

  it("renders privacy banner with .mcp-banner.local class", () => {
    const el = makeContainer();
    renderPrivacySummary(el, [], vi.fn());
    const banner = el.querySelector(".mcp-banner.local");
    expect(banner).not.toBeNull();
    expect(banner.textContent).toContain("By default, everything runs on your Mac.");
  });
});
