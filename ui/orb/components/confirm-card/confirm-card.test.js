import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({ daemon: { confirm: vi.fn().mockResolvedValue({}) } }));
import { daemon } from "../../lib/daemon.js";
import { showConfirm, clearConfirm } from "./confirm-card.js";

function makeLog() { const log = document.createElement("div"); log.scroll = () => {}; document.body.appendChild(log); return log; }
beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = ""; });

it("renders a danger card with the right header", () => {
  const log = makeLog();
  showConfirm(log, "Delete it?", "danger");
  const card = log.querySelector("#confirm-card");
  expect(card.classList.contains("danger")).toBe(true);
  expect(card.querySelector(".h").textContent).toBe("⚠️ Just checking");
  expect(card.querySelector(".b").textContent).toBe("Delete it?");
});

it("yes posts {value:'yes'} and removes the card", () => {
  const log = makeLog();
  showConfirm(log, "ok?", "write");
  log.querySelector('[data-v="yes"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "yes" });
  expect(log.querySelector("#confirm-card")).toBeNull();
});

it("with options, yes posts the selected option value", () => {
  const log = makeLog();
  showConfirm(log, "pick", "read", [{ value: "a", label: "A" }, { value: "b", label: "B" }]);
  log.querySelector("#confSel").value = "b";
  log.querySelector('[data-v="yes"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "b" });
});

it("clearConfirm removes an existing card", () => {
  const log = makeLog();
  showConfirm(log, "x", "danger");
  clearConfirm(log);
  expect(log.querySelector("#confirm-card")).toBeNull();
});
