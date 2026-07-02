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

it("with options, each option renders a button that posts its value", () => {
  const log = makeLog();
  showConfirm(log, "pick", "read", [{ value: "once", label: "Allow once" }, { value: "session", label: "Allow this session" }]);
  const card = log.querySelector("#confirm-card");
  expect(card.querySelector('[data-v="session"]').textContent).toBe("Allow this session");
  card.querySelector('[data-v="once"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "once" });
});

it("options card has a Cancel button that posts no", () => {
  const log = makeLog();
  showConfirm(log, "pick", "danger", [{ value: "once", label: "Allow once" }, { value: "session", label: "Session" }]);
  log.querySelector('[data-v="no"]').click();
  expect(daemon.confirm).toHaveBeenCalledWith({ value: "no" });
});

it("clearConfirm removes an existing card", () => {
  const log = makeLog();
  showConfirm(log, "x", "danger");
  clearConfirm(log);
  expect(log.querySelector("#confirm-card")).toBeNull();
});

it("network card has class 'network'", () => {
  const log = makeLog();
  showConfirm(log, "Send data?", "network");
  const card = log.querySelector("#confirm-card");
  expect(card.classList.contains("network")).toBe(true);
});

it("network card heading is 'Allow network action'", () => {
  const log = makeLog();
  showConfirm(log, "Send data?", "network");
  const card = log.querySelector("#confirm-card");
  expect(card.querySelector(".h").textContent).toBe("Allow network action");
});

it("network card with serverLabel renders connection badge", () => {
  const log = makeLog();
  showConfirm(log, "Send?", "network", undefined, { serverLabel: "Slack" });
  const card = log.querySelector("#confirm-card");
  const kvConn = Array.from(card.querySelectorAll(".kv")).find(kv => kv.querySelector(".k").textContent === "Connection");
  expect(kvConn).toBeTruthy();
  expect(kvConn.querySelector(".srvbadge").textContent).toBe("Slack");
});

it("network card with egress renders data path row", () => {
  const log = makeLog();
  showConfirm(log, "Send?", "network", undefined, { egress: "text sent to slack.com" });
  const card = log.querySelector("#confirm-card");
  const kvPath = Array.from(card.querySelectorAll(".kv")).find(kv => kv.querySelector(".k").textContent === "Data path");
  expect(kvPath).toBeTruthy();
  expect(kvPath.querySelector(".egress").textContent).toBe("↗ text sent to slack.com");
});

it("network card with both serverLabel and egress renders both rows", () => {
  const log = makeLog();
  showConfirm(log, "Send?", "network", undefined, { serverLabel: "Slack", egress: "text sent to slack.com" });
  const card = log.querySelector("#confirm-card");
  const kvs = card.querySelectorAll(".kv");
  expect(kvs.length).toBe(2);
  expect(Array.from(kvs).some(kv => kv.querySelector(".k").textContent === "Connection")).toBe(true);
  expect(Array.from(kvs).some(kv => kv.querySelector(".k").textContent === "Data path")).toBe(true);
});

it("existing danger card still works (no regression)", () => {
  const log = makeLog();
  showConfirm(log, "Delete it?", "danger");
  const card = log.querySelector("#confirm-card");
  expect(card.classList.contains("danger")).toBe(true);
  expect(card.querySelector(".h").textContent).toBe("⚠️ Just checking");
  expect(card.querySelector(".kv")).toBeNull();
});
