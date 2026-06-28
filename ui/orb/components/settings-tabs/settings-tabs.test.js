import { describe, it, expect, vi } from "vitest";
import "./settings-tabs.js";

function mount() {
  document.body.innerHTML = `
    <settings-tabs class="tabs">
      <button data-tab="model" class="active">Model</button>
      <button data-tab="perms">Permissions</button>
    </settings-tabs>
    <section class="panel active" id="tab-model"></section>
    <section class="panel" id="tab-perms"></section>`;
  return document.querySelector("settings-tabs");
}

it("activates the matching panel and fires tab-change", () => {
  const tabs = mount();
  const spy = vi.fn();
  tabs.addEventListener("tab-change", (e) => spy(e.detail));
  tabs.querySelector('[data-tab="perms"]').click();
  expect(document.getElementById("tab-perms").classList.contains("active")).toBe(true);
  expect(document.getElementById("tab-model").classList.contains("active")).toBe(false);
  expect(spy).toHaveBeenCalledWith("perms");
});
