import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/tauri.js", () => ({ openExternal: vi.fn() }));
import "./chat-log.js";

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
