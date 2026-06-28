/** Segmented tab switcher for the settings window. Toggles `.panel.active` and emits
 *  a `tab-change` CustomEvent (detail = tab name) so the page can lazy-load a tab. */
export class SettingsTabs extends HTMLElement {
  connectedCallback() {
    // Delegate so it works regardless of when child buttons are parsed/attached.
    this.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tab]");
      if (b && this.contains(b)) this.select(b.dataset.tab);
    });
  }
  select(tab) {
    this.querySelectorAll("button[data-tab]").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    const panel = document.getElementById("tab-" + tab);
    if (panel) panel.classList.add("active");
    this.dispatchEvent(new CustomEvent("tab-change", { detail: tab, bubbles: true }));
  }
}
customElements.define("settings-tabs", SettingsTabs);
