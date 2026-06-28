import { describe, it, expect, beforeEach } from "vitest";
import { setupContextMeter } from "./context-meter.js";

function mount() {
  document.body.innerHTML = `
    <span id="ctx"><svg><circle id="ctxArc" stroke-dashoffset="94.2"/></svg><span id="ctxPct">0%</span></span>
    <div id="ctxDetail" class="hidden"></div>`;
}
beforeEach(() => { mount(); });

it("update sets the percent and shows the ring", () => {
  const m = setupContextMeter();
  m.update({ pct: 42, used: 1000, window: 2000, model: "claude-haiku-4-5" });
  expect(document.getElementById("ctxPct").textContent).toBe("42%");
  expect(document.getElementById("ctx").classList.contains("show")).toBe(true);
});

it("applies the danger color above 85%", () => {
  const m = setupContextMeter();
  m.update({ pct: 90 });
  expect(document.getElementById("ctxArc").getAttribute("stroke")).toBe("var(--danger)");
  expect(document.getElementById("ctxPct").style.color).toBe("var(--danger)");
});

it("clicking the ring opens the detail card and renders cost when present", () => {
  const m = setupContextMeter();
  m.update({ pct: 50, used: 100, window: 200, turn_in: 10, turn_out: 5, price: 0.5, model: "qwen3:8b" });
  document.getElementById("ctx").click();
  const d = document.getElementById("ctxDetail");
  expect(d.classList.contains("hidden")).toBe(false);
  expect(d.innerHTML).toContain("$0.5000");
});

it("reset hides the ring and zeroes the percent", () => {
  const m = setupContextMeter();
  m.update({ pct: 50 });
  m.reset();
  expect(document.getElementById("ctx").classList.contains("show")).toBe(false);
  expect(document.getElementById("ctxPct").textContent).toBe("0%");
});
