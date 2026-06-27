import { describe, it, expect } from "vitest";

describe("test harness", () => {
  it("runs in a DOM environment", () => {
    const el = document.createElement("div");
    el.textContent = "ok";
    expect(el.textContent).toBe("ok");
  });

  it("supports custom elements", () => {
    expect(typeof customElements).toBe("object");
    expect(typeof customElements.define).toBe("function");
  });
});
