/** Voice-models status + on-demand download with a live progress bar. Keeps its OWN
 *  short-lived WebSocket (opened before POSTing so no early `voice_download` frames are
 *  missed — the daemon bus replays only state, not these events), with a watchdog (slide
 *  the bar when a stage can't report bytes) and a status poll as a done-frame fallback.
 *  Moved verbatim from settings.html. Wraps the voice card; manages only its own ids. */
import { daemon } from "../../lib/daemon.js";
import { $ } from "../../lib/dom.js";

export class VoiceDownload extends HTMLElement {
  #downloading = false;
  #poll = null;
  #watch = null;
  #ws = null;
  #lastPct = -1;
  #lastAt = 0;

  connectedCallback() {
    this.addEventListener("click", (e) => { if (e.target.closest("#voiceDownloadBtn")) this.start(); });
  }

  async loadStatus() {
    const desc = $("voiceStatusDesc"), badge = $("voiceBadge"), btn = $("voiceDownloadBtn");
    // The progress row is only for an in-flight download — keep it hidden otherwise.
    if (!this.#downloading) { $("voiceProgressRow").style.display = "none"; }
    let s;
    try { s = await daemon.voiceStatus(); }
    catch (e) { desc.textContent = "Couldn't reach Jack — is it running?"; badge.textContent = "—"; return; }
    if (s.ready) {
      badge.textContent = "Ready"; badge.className = "badge granted";
      desc.textContent = "Downloaded — voice is ready to use.";
      btn.style.display = "none";
    } else {
      const models = s.models || {}, miss = (s.needed || []).filter((k) => !models[k]);
      badge.textContent = "Not installed"; badge.className = "badge needed";
      desc.textContent = "Needs downloading: " + (miss.join(", ") || "voice models") + ". Download to enable voice.";
      btn.style.display = "";
    }
  }

  #cleanup() {
    if (this.#poll) { clearInterval(this.#poll); this.#poll = null; }
    if (this.#watch) { clearInterval(this.#watch); this.#watch = null; }
    if (this.#ws) { try { this.#ws.close(); } catch (e) {} this.#ws = null; }
  }

  #finish(errored) {
    if (!this.#downloading) return; // guard: done frame + poll can both fire
    this.#downloading = false;
    this.#cleanup();
    const row = $("voiceProgressRow"), bar = $("voiceProgressBar"), btn = $("voiceDownloadBtn");
    btn.disabled = false; bar.classList.remove("slide");
    if (!errored) { bar.style.width = "100%"; }
    setTimeout(() => { if (!errored) { row.style.display = "none"; } this.loadStatus(); }, errored ? 0 : 500);
  }

  start() {
    const btn = $("voiceDownloadBtn"), row = $("voiceProgressRow"), bar = $("voiceProgressBar"), lbl = $("voiceProgressLabel");
    if (this.#downloading) return; // ignore double-clicks
    this.#downloading = true; this.#lastPct = -1; this.#lastAt = Date.now();
    btn.disabled = true; row.style.display = "flex";
    bar.classList.remove("slide"); bar.style.width = "0%"; lbl.textContent = "Starting…";

    let posted = false;
    const postOnce = () => { if (posted) return; posted = true; daemon.voiceDownload().catch(() => {}); };

    // Open the progress socket BEFORE kicking off the download so no early frames are missed.
    try { this.#ws = new WebSocket(daemon.wsUrl); } catch (e) { this.#ws = null; }
    if (this.#ws) {
      this.#ws.onopen = postOnce;
      this.#ws.onerror = postOnce; // socket failed — still start; polling will track it
      this.#ws.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.type !== "voice_download") return;
        if (m.error) { lbl.textContent = "Failed: " + m.error; this.#finish(true); return; }
        const pct = (typeof m.pct === "number") ? m.pct : 0;
        if (pct > this.#lastPct) { // real progress — show a determinate fill
          this.#lastPct = pct; this.#lastAt = Date.now();
          bar.classList.remove("slide"); bar.style.width = pct + "%";
        }
        lbl.textContent = m.stage || "Downloading…";
        if (m.done) { this.#finish(false); }
      };
    } else {
      postOnce();
    }

    // Watchdog: if a stage stops reporting (e.g. the big STT model), slide the bar.
    this.#watch = setInterval(() => {
      if (this.#downloading && (Date.now() - this.#lastAt) > 1500) { bar.classList.add("slide"); }
    }, 600);

    // Fallback: poll status so we still resolve to "Ready" if the done frame is missed.
    this.#poll = setInterval(async () => {
      try { const s = await daemon.voiceStatus(); if (s && s.ready) { this.#finish(false); } }
      catch (e) { /* keep polling */ }
    }, 3000);
  }
}
customElements.define("voice-download", VoiceDownload);
