import { describe, it, expect, vi } from "vitest";
import { $, el, on, pointCaretAt } from "./dom.js";

it("$ finds by id", () => {
  document.body.innerHTML = '<div id="x">hi</div>';
  expect($("x").textContent).toBe("hi");
});

it("el builds an element with props and children", () => {
  const node = el("button", { className: "btn", id: "go" }, "Click");
  expect(node.tagName).toBe("BUTTON");
  expect(node.className).toBe("btn");
  expect(node.id).toBe("go");
  expect(node.textContent).toBe("Click");
});

it("el accepts an array of children", () => {
  const node = el("div", {}, [el("span", {}, "a"), "b"]);
  expect(node.childNodes.length).toBe(2);
  expect(node.textContent).toBe("ab");
});

it("on adds and the returned fn removes the listener", () => {
  const node = el("button");
  const handler = vi.fn();
  const off = on(node, "click", handler);
  node.click(); expect(handler).toHaveBeenCalledTimes(1);
  off(); node.click(); expect(handler).toHaveBeenCalledTimes(1);
});

it("pointCaretAt sets the --caret-x custom property (clamped, in px)", () => {
  const pop = el("div"), trig = el("div");
  document.body.append(pop, trig);
  pointCaretAt(pop, trig);
  expect(pop.style.getPropertyValue("--caret-x")).toMatch(/^\d+px$/);
});

it("pointCaretAt is a no-op when an element is missing", () => {
  expect(() => pointCaretAt(null, el("div"))).not.toThrow();
});
