import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({ daemon: { action: vi.fn().mockResolvedValue({ result: "Done" }) } }));
vi.mock("../../lib/clipboard.js", () => ({ copyText: vi.fn().mockResolvedValue(true) }));
import { daemon } from "../../lib/daemon.js";
import { copyText } from "../../lib/clipboard.js";
import { showChoices } from "./choices-card.js";

function makeLog() { const log = document.createElement("div"); log.scroll = () => {}; document.body.appendChild(log); return log; }
beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = ""; });

it("renders top 5 items plus a '+N more' footer", () => {
  const log = makeLog();
  const items = [1, 2, 3, 4, 5, 6, 7].map((n) => ({ label: "f" + n, actions: [] }));
  showChoices(log, { title: "Files", items });
  const card = log.querySelector(".choices");
  expect(card.querySelectorAll(".it").length).toBe(5);
  expect(card.querySelector(".more").textContent).toContain("+2 more");
});

it("a copy action copies client-side", () => {
  const log = makeLog();
  showChoices(log, { title: "x", items: [{ label: "f", actions: [{ copy: "/path/x", label: "Copy path" }] }] });
  log.querySelector(".ia .btn").click();
  expect(copyText).toHaveBeenCalledWith("/path/x");
});

it("a tool action runs through daemon.action", () => {
  const log = makeLog();
  showChoices(log, { title: "x", items: [{ label: "f", actions: [{ tool: "open", args: { i: 1 }, label: "Open" }] }] });
  log.querySelector(".ia .btn").click();
  expect(daemon.action).toHaveBeenCalledWith("open", { i: 1 });
});
