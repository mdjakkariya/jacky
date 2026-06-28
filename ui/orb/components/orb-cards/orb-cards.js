/** Notification-style card stack shown under the orb (voice mode): confirmation
 *  alerts + voice-search choices. Moved from index.html. POSTs go through lib/daemon;
 *  the page injects window-sizing + show-orb hooks (Tauri-only) via properties:
 *    el.enterCardMode / el.exitCardMode / el.ensureOrb  (all optional, default no-op).
 *  The element IS the cards container (use <orb-cards id="cards">). */
import { daemon } from "../../lib/daemon.js";

export class OrbCards extends HTMLElement {
  enterCardMode = () => {};
  exitCardMode = () => {};
  ensureOrb = () => {};

  connectedCallback() {
    // Off-screen live region for VoiceOver; reuse the page's #a11y-live if present.
    this._onKeydown = (e) => this.#onKeydown(e);
    document.addEventListener("keydown", this._onKeydown);
  }
  disconnectedCallback() {
    document.removeEventListener("keydown", this._onKeydown);
  }

  // Announce text to screen readers via the off-screen assertive live region.
  #announce(text) {
    const el = document.getElementById("a11y-live"); if (!el) return;
    el.textContent = ""; setTimeout(() => { el.textContent = text || ""; }, 40);
  }

  #cardsPresent() { return this.children.length > 0; }

  /** Confirmation card (accessible alertdialog). Answer by click/keyboard (or voice
   *  upstream). Focus defaults to the SAFE choice on destructive asks. */
  showConfirm(text, kind) {
    try { this.ensureOrb(); } catch (e) {}
    const old = this.querySelector("#confirm-card"); if (old) old.remove();
    kind = kind || "danger";
    const ttl = kind === "read" ? "Allow access" : (kind === "write" ? "Allow change" : "Confirm");
    const icon = kind === "read" ? "🔒" : (kind === "write" ? "✎" : "⚠️");
    const yes = kind === "danger" ? "Proceed" : "Allow";
    const no = kind === "danger" ? "Cancel" : "Not now";
    const el = document.createElement("div"); el.className = "card " + kind; el.id = "confirm-card";
    el.setAttribute("role", "alertdialog");
    el.setAttribute("aria-labelledby", "cf-ttl");
    el.setAttribute("aria-describedby", "cf-msg");
    el.innerHTML = '<div class="hd"><div class="ic" aria-hidden="true"></div>'
      + '<div class="tx"><div class="ttl" id="cf-ttl"></div><div class="msg" id="cf-msg"></div></div></div>'
      + '<div class="row"><button type="button" class="btn no"></button>'
      + '<button type="button" class="btn yes"></button></div>'
      + '<div class="hint">…or say “' + (kind === "danger" ? "proceed" : "allow") + '” / “cancel”.</div>';
    el.querySelector(".ic").textContent = icon;
    el.querySelector(".ttl").textContent = ttl;
    el.querySelector(".msg").textContent = text || "Are you sure?";
    const noBtn = el.querySelector(".no"), yesBtn = el.querySelector(".yes");
    noBtn.textContent = no; yesBtn.textContent = yes;
    noBtn.setAttribute("aria-label", no + " — " + ttl);
    yesBtn.setAttribute("aria-label", yes + " — " + ttl);
    yesBtn.addEventListener("click", () => this.#sendConfirm(true));
    noBtn.addEventListener("click", () => this.#sendConfirm(false));
    this.appendChild(el);
    this.enterCardMode();
    this.#announce(ttl + ". " + (text || ""));
    // Destructive → focus Cancel (a stray Enter shouldn't delete); else the action.
    setTimeout(() => { (kind === "danger" ? noBtn : yesBtn).focus(); }, 50);
  }

  #sendConfirm(answer) {
    daemon.confirm({ answer }).catch(() => {});
    this.clear(); // optimistic; the engine also sends confirm_clear
  }

  clear() {
    this.innerHTML = "";
    this.exitCardMode();
  }

  clearChoices() {
    const el = this.querySelector("#choices-card");
    if (el) { el.remove(); if (!this.#cardsPresent()) this.exitCardMode(); }
  }

  #runChoiceAction(act, btn, statusEl, row) {
    row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    statusEl.textContent = "Working…";
    daemon.action(act.tool, act.args || {})
      .then((r) => { statusEl.textContent = (r && r.result) ? r.result : "Done"; })
      .catch(() => { statusEl.textContent = "Couldn't do that"; })
      .finally(() => { row.querySelectorAll("button").forEach((b) => { b.disabled = false; }); });
  }

  /** Voice search preview: top matches, each with actions. */
  showChoices(msg) {
    if (!msg || !msg.items || !msg.items.length) return;
    try { this.ensureOrb(); } catch (e) {}
    this.clearChoices();
    const top = msg.items.slice(0, 3); // a glanceable preview — say/scroll for the rest
    const el = document.createElement("div"); el.className = "card"; el.id = "choices-card";
    el.setAttribute("role", "group");
    el.setAttribute("aria-labelledby", "ch-ttl");
    el.innerHTML = '<div class="hd"><div class="ic" aria-hidden="true">🔎</div>'
      + '<div class="tx"><div class="ttl" id="ch-ttl"></div></div></div>'
      + '<div class="list"></div>';
    el.querySelector(".ttl").textContent = msg.title || "Top matches";
    const list = el.querySelector(".list");
    top.forEach((item, i) => {
      const it = document.createElement("div"); it.className = "citem"; it.setAttribute("role", "group");
      const name = document.createElement("div"); name.className = "cname";
      name.textContent = (i + 1) + ". " + (item.label || ""); it.appendChild(name);
      if (item.sublabel) { const s = document.createElement("div"); s.className = "csub"; s.textContent = item.sublabel; it.appendChild(s); }
      const row = document.createElement("div"); row.className = "crow";
      const status = document.createElement("div"); status.className = "cstatus";
      status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
      (item.actions || []).forEach((act) => {
        if (act.copy != null) return; // "copy path" is a chat affordance; skip on the orb
        const b = document.createElement("button"); b.type = "button"; b.className = "btn"; b.textContent = act.label || "Open";
        b.setAttribute("aria-label", (act.label || "Open") + " " + (item.label || "file"));
        b.addEventListener("click", () => this.#runChoiceAction(act, b, status, row));
        row.appendChild(b);
      });
      it.appendChild(row); it.appendChild(status); list.appendChild(it);
    });
    const more = msg.items.length - top.length;
    const hint = document.createElement("div"); hint.className = "hint";
    hint.textContent = (more > 0 ? ("+" + more + " more · ") : "") + "say “open the first one”.";
    el.appendChild(hint);
    this.appendChild(el);
    this.enterCardMode();
    this.#announce((msg.title || "Top matches") + ", " + top.length + (top.length === 1 ? " result." : " results."));
    setTimeout(() => { const b = el.querySelector(".btn"); if (b) b.focus(); }, 50);
  }

  // Esc cancels a confirmation / dismisses a preview; Tab is trapped inside the open card.
  #onKeydown(e) {
    const confirm = this.querySelector("#confirm-card");
    const choices = this.querySelector("#choices-card");
    const card = confirm || choices; if (!card) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (confirm) this.#sendConfirm(false); else this.clearChoices();
      return;
    }
    if (e.key === "Tab") {
      const btns = card.querySelectorAll("button"); if (!btns.length) return;
      const first = btns[0], last = btns[btns.length - 1], a = document.activeElement;
      if (e.shiftKey && (a === first || !card.contains(a))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (a === last || !card.contains(a))) { e.preventDefault(); first.focus(); }
    }
  }
}

customElements.define("orb-cards", OrbCards);
