/** One-line "update available" banner. Checks GitHub Releases (anonymous, public repo —
 *  no user data sent) and shows itself only when a newer version than the running one is
 *  published and not already dismissed. Wraps #updateBanner. Moved from chat.html. */
import { cmpVer } from "../../lib/format.js";
import { appVersion, openExternal } from "../../lib/tauri.js";

const UPDATE_REPO = "mdjakkariya/jacky";

export class UpdateBanner extends HTMLElement {
  async check() {
    const cur = await appVersion();
    let rel;
    try { rel = await (await fetch("https://api.github.com/repos/" + UPDATE_REPO + "/releases/latest")).json(); }
    catch (e) { return; }
    if (!rel || !rel.tag_name) return;
    const latest = String(rel.tag_name).replace(/^v/, "");
    if (cmpVer(latest, cur) <= 0) return; // already current
    try { if (localStorage.getItem("jackUpdateDismissed") === latest) return; } catch (e) {}
    this.#show(latest, rel.html_url || ("https://github.com/" + UPDATE_REPO + "/releases/latest"));
  }

  #show(latest, url) {
    const ack = () => { // mark this version seen so the banner doesn't return for it
      this.classList.add("hidden");
      try { localStorage.setItem("jackUpdateDismissed", latest); } catch (e) {}
    };
    this.querySelector(".u-text").textContent = "Jack " + latest + " is available";
    this.querySelector(".u-link").onclick = () => { openExternal(url); ack(); }; // opening counts as ack
    this.querySelector(".u-dismiss").onclick = ack;
    this.classList.remove("hidden");
  }
}
customElements.define("update-banner", UpdateBanner);
