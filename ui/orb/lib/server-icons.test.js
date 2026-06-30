import { describe, it, expect } from "vitest";
import { serverIconEl } from "./server-icons.js";

describe("serverIconEl", () => {
  it("returns an SVG brand logo for known servers", () => {
    for (const id of ["github", "slack", "notion"]) {
      const el = serverIconEl(id);
      expect(el.tagName.toLowerCase()).toBe("svg");
      expect(el.querySelectorAll("path").length).toBeGreaterThan(0);
    }
  });

  it("Slack logo carries its brand colours", () => {
    const el = serverIconEl("slack");
    const fills = [...el.querySelectorAll("path")].map((p) => p.getAttribute("fill"));
    expect(fills).toContain("#E01E5A");
    expect(fills).toContain("#36C5F0");
  });

  it("monochrome marks use currentColor (theme-adaptive)", () => {
    const el = serverIconEl("github");
    expect(el.querySelector("path").getAttribute("fill")).toBe("currentColor");
  });

  it("falls back to a folder glyph for local files", () => {
    const el = serverIconEl("files");
    expect(el.tagName.toLowerCase()).toBe("span");
    expect(el.textContent).toBe("📁");
  });

  it("falls back to a plug glyph for unknown/custom servers", () => {
    expect(serverIconEl("custom-1730000000000").textContent).toBe("🔌");
  });
});
