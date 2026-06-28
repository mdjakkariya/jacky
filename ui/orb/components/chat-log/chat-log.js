/** The chat transcript (the scroll container itself). Owns message bubbles (with
 *  dependency-free markdown for Jack), the typing indicator, smart scroll + a jump
 *  button, the live tool-step trace, long-paste clamping, and the fresh-session
 *  welcome (whose suggestion chips emit a `chip-send` CustomEvent). Moved from
 *  chat.html. Use <chat-log id="log">…welcome markup…</chat-log>. */
import { renderMarkdown } from "../../lib/markdown.js";
import { openExternal } from "../../lib/tauri.js";

export class ChatLog extends HTMLElement {
  connectedCallback() {
    // Keep a pristine clone of the welcome block as the template for new chats.
    const empty = this.querySelector("#empty");
    if (empty && !this._emptyTpl) this._emptyTpl = empty.cloneNode(true);
    this.addEventListener("scroll", () => this.updateJump());
    this._bindChips(this); // wire the chips already in the DOM
    queueMicrotask(() => this._wireJump());
  }

  _wireJump() {
    const jump = document.getElementById("jump");
    if (jump && !jump._wired) { jump._wired = true; jump.addEventListener("click", () => this.toBottom()); }
    this.updateJump();
  }

  // --- smart scroll ---------------------------------------------------------
  nearBottom() { return this.scrollHeight - this.scrollTop - this.clientHeight < 80; }
  updateJump() { const j = document.getElementById("jump"); if (j) j.classList.toggle("hidden", this.nearBottom()); }
  toBottom() { this.scrollTop = this.scrollHeight; this.updateJump(); }
  scroll() { if (this.nearBottom()) this.scrollTop = this.scrollHeight; this.updateJump(); }

  // Collapse very long user pastes behind a "Show more".
  _clampIfLong(d, cls) {
    if (cls !== "me") return; // only collapse long user pastes; never Jack's replies
    if (d.scrollHeight <= 160) return;
    d.classList.add("clamped");
    const b = document.createElement("button"); b.className = "more"; b.textContent = "Show more";
    b.addEventListener("click", () => {
      const clamped = d.classList.toggle("clamped");
      b.textContent = clamped ? "Show more" : "Show less";
      this.updateJump();
    });
    d.insertAdjacentElement("afterend", b);
  }

  // --- welcome / empty state ------------------------------------------------
  removeEmpty() { const e = this.querySelector("#empty"); if (e) e.remove(); }
  _bindChips(root) {
    root.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("chip-send", { detail: chip.getAttribute("data-send") || "", bubbles: true }));
      });
    });
  }
  showEmpty() {
    this.innerHTML = "";
    if (!this._emptyTpl) return;
    const n = this._emptyTpl.cloneNode(true);
    this.appendChild(n); this._bindChips(n);
  }
  showInitializing() {
    this.innerHTML = '<div class="empty">'
      + '<div class="spinner" aria-hidden="true" style="margin-bottom:16px"></div>'
      + '<h2>Starting Jack…</h2>'
      + '<p>Getting things ready — just a moment.</p></div>';
  }

  // --- message bubbles ------------------------------------------------------
  bubble(cls, text, md) {
    this.removeEmpty();
    const d = document.createElement("div"); d.className = "msg " + cls;
    if (md) {
      d.innerHTML = renderMarkdown(text);
      d.querySelectorAll("a.mdlink").forEach((a) => {
        a.addEventListener("click", (e) => { e.preventDefault(); openExternal(a.getAttribute("href")); });
      });
    } else {
      d.textContent = text;
    }
    this.appendChild(d); this._clampIfLong(d, cls); this.scroll(); return d;
  }

  // --- typing indicator (only ever one) -------------------------------------
  showTyping() {
    if (!this._typingEl) {
      this._typingEl = document.createElement("div"); this._typingEl.className = "typing";
      this._typingEl.setAttribute("aria-label", "Jack is typing");
      this._typingEl.innerHTML = "<span></span><span></span><span></span>";
      this.appendChild(this._typingEl);
    }
    this.scroll();
  }
  hideTyping() { this.clearSteps(); if (this._typingEl) { this._typingEl.remove(); this._typingEl = null; } }

  // --- live tool-step trace -------------------------------------------------
  renderStep(m) {
    if (!this._stepTrace) {
      this._stepTrace = document.createElement("div"); this._stepTrace.className = "steptrace";
      this.appendChild(this._stepTrace);
    }
    let row = this._stepTrace.querySelector('[data-i="' + m.index + '"]');
    if (!row) {
      row = document.createElement("div");
      row.setAttribute("data-i", m.index);
      row.innerHTML = '<span class="dot"></span><span class="label"></span>';
      this._stepTrace.appendChild(row);
    }
    row.className = "row " + (m.status || "running");
    const suffix = m.status === "done" ? " ✓" : m.status === "failed" ? " ✗" : "…";
    row.querySelector(".label").textContent = (m.label || m.tool) + suffix;
    this.scrollTop = this.scrollHeight;
  }
  clearSteps() { if (this._stepTrace) { this._stepTrace.remove(); this._stepTrace = null; } }
}
customElements.define("chat-log", ChatLog);
