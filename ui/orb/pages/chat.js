/** Chat drawer orchestration: composer, one-turn locking, mode switch, global
 *  shortcuts, window drag, the startup gate, and WS wiring to the components.
 *  Moved from chat.html; transcript/cards/meter/chip/banner now live in their own
 *  modules, and all daemon/clipboard/tauri access goes through lib. */
import { daemon } from "../lib/daemon.js";
import { $ } from "../lib/dom.js";
import { copyText } from "../lib/clipboard.js";
import { closeChat, hideChat, openSettingsVoice, tauriWindow } from "../lib/tauri.js";
import { createEarcons } from "../lib/earcons.js";
import "../components/chat-log/chat-log.js";
import "../components/update-banner/update-banner.js";
import { showConfirm, clearConfirm } from "../components/confirm-card/confirm-card.js";
import { showChoices } from "../components/choices-card/choices-card.js";
import { setupContextMeter } from "../components/context-meter/context-meter.js";
import { setupFolderChip } from "../components/folder-chip/folder-chip.js";

const log = $("log"), box = $("box"), send = $("send");
const earcons = createEarcons({ gain: 0.16 });
earcons.resumeOnGesture();
const ctxMeter = setupContextMeter();
const folderChip = setupFolderChip();

// --- one turn at a time -----------------------------------------------------
let busy = false;
function lockInput(on) {
  busy = on;
  box.disabled = on;
  send.disabled = on || !box.value.trim();
}

async function submit() {
  const text = box.value.trim();
  if (busy || !text) return;
  box.value = ""; resize(); updateCounter();
  phStop(); // the chat has started — stop rotating hints
  log.bubble("me", text);
  log.toBottom();
  lockInput(true);
  log.showTyping();
  try {
    const r = await daemon.chat(text);
    log.bubble("jack", (r && r.reply) ? r.reply : "(no reply)", !!(r && r.reply));
  } catch (e) {
    log.bubble("jack", "Couldn't reach Jack — is it running?");
  } finally {
    log.hideTyping();
    clearConfirm(log);
    lockInput(false);
  }
}

// --- composer: autosize + char counter --------------------------------------
function resize() { box.style.height = "26px"; box.style.height = Math.min(130, box.scrollHeight) + "px"; }
const MAXLEN = 4000, WARN_AT = 200, counter = $("counter");
function updateCounter() {
  const left = MAXLEN - box.value.length;
  if (left > WARN_AT) { counter.classList.remove("show"); return; }
  counter.classList.add("show");
  counter.classList.toggle("warn", left <= 0);
  counter.textContent = left > 0 ? (left + " left") : "Limit reached";
}
box.addEventListener("input", () => { resize(); updateCounter(); send.disabled = busy || !box.value.trim(); phSync(); });

// --- rotating placeholder hints (only while the chat is untouched) ----------
const phHint = $("phHint");
const PH_HINTS = ["Message Jack…", "⌘⌃J to summon or hide", "⌘⌃C chat · ⌘⌃V voice"];
let phIdx = 0, phStarted = false, phTimer = null;
function phSync() { phHint.style.opacity = box.value ? "0" : "1"; }
function phResting() { return !phStarted && !box.value; }
function phStop() { phStarted = true; phHint.textContent = "Message Jack…"; phSync(); }
function phStep() {
  if (!phResting()) return;
  phIdx = (phIdx + 1) % PH_HINTS.length;
  phHint.style.opacity = "0";
  setTimeout(() => { phHint.textContent = PH_HINTS[phIdx]; phHint.style.opacity = phResting() ? "1" : "0"; }, 300);
}
function phStart() { phStarted = false; phIdx = 0; phHint.textContent = "Message Jack…"; phSync(); if (phTimer) clearInterval(phTimer); phTimer = setInterval(phStep, 6000); }
phStart();

box.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
send.addEventListener("click", submit);
log.addEventListener("chip-send", (e) => { if (busy) return; box.value = e.detail || ""; resize(); updateCounter(); submit(); });

// --- new chat ---------------------------------------------------------------
async function newChat() {
  if (busy) return; // a turn is in flight — don't wipe mid-thought
  try { await daemon.newSession(); } catch (e) {}
  log.hideTyping(); clearConfirm(log);
  log.showEmpty(); ctxMeter.reset();
  box.value = ""; resize(); updateCounter(); lockInput(false); phStart(); box.focus();
}
$("newchat").addEventListener("click", newChat);

// --- daemon WS wiring -------------------------------------------------------
daemon.on("confirm", (m) => showConfirm(log, m.text, m.kind, m.options));
daemon.on("confirm_clear", () => clearConfirm(log));
daemon.on("context", (m) => ctxMeter.update(m));
daemon.on("choices", (m) => { if (m.mode !== "voice") showChoices(log, m); });
daemon.on("step", (m) => log.renderStep(m));
daemon.on("workspace", (m) => folderChip.renderFromEvent(m));
daemon.connect();
folderChip.refresh(); // populate chip (and modal if open) on load

// --- update notify ----------------------------------------------------------
$("updateBanner").check();

// --- dev-only "copy debug report" ------------------------------------------
async function copyDebugReport() {
  const btn = $("dbg"), tip = $("dbgTip");
  let txt = "";
  try { txt = (await daemon.reportConcise()).report || ""; } catch (e) {}
  const ok = !!txt && await copyText(txt);
  tip.textContent = ok ? "Copied report" : "Couldn't copy";
  btn.classList.add("copied");
  setTimeout(() => { btn.classList.remove("copied"); }, 1500);
}
$("dbg").addEventListener("click", copyDebugReport);
// Reveal the button in dev builds; retry because the daemon may still be starting.
async function refreshDebugButton() {
  for (let i = 0; i < 5; i++) {
    try {
      const s = await daemon.settings();
      if (s && s.show_debug) { $("dbg").classList.remove("hidden"); }
      return; // got a definite answer — stop retrying
    } catch (e) { await new Promise((res) => { setTimeout(res, 800); }); }
  }
}
refreshDebugButton();

// --- interaction mode + voice toggle ---------------------------------------
async function setMode(mode) { try { await daemon.setSettings({ interaction_mode: mode }); } catch (e) {} }
async function voiceReady() { try { return !!(await daemon.voiceStatus()).ready; } catch (e) { return false; } }
async function enableVoice() {
  if (await voiceReady()) {
    await setMode("voice");
    // The "switched to voice" cue is played by the orb (the visible surface then) via close_chat.
    closeChat();
  } else {
    openSettingsVoice(); // deep-link to the voice download
  }
}
// ✕ just hides/closes the drawer (stays in chat mode); summon back with ⌘⌃J.
async function closeDrawer() { await setMode("chat"); hideChat(); }

$("m-voice").addEventListener("click", enableVoice);
window.__enableVoice = enableVoice; // driven by the ⌘⌃V global shortcut / tray "Voice…"
$("m-chat").addEventListener("click", () => { earcons.playMode("chat"); setMode("chat"); });
$("close").addEventListener("click", closeDrawer);

// Esc or ⌘W closes the drawer.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closeDrawer(); }
  else if ((e.metaKey || e.ctrlKey) && (e.key === "w" || e.key === "W")) { e.preventDefault(); closeDrawer(); }
});

// Drag the window by its header (CSS app-region is unreliable on frameless macOS).
const header = document.querySelector("header");
if (header) {
  header.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest("button, .seg")) return;
    const w = tauriWindow();
    if (w && w.getCurrentWindow) { w.getCurrentWindow().startDragging().catch(() => {}); }
  });
}

// Opening the drawer = entering chat mode (pauses the voice loop). Re-assert on focus.
setMode("chat");
window.addEventListener("focus", () => setMode("chat"));

// --- startup gate: wait for the daemon before enabling the composer ---------
async function waitForReady() {
  log.showInitializing();
  lockInput(true);
  for (let i = 0; i < 600; i++) { // ~8 min ceiling; the daemon normally answers in <2s
    try { if ((await daemon.healthz()).ok) { break; } } catch (e) {}
    await new Promise((res) => { setTimeout(res, 800); });
  }
  log.showEmpty();
  lockInput(false);
  box.focus();
  refreshDebugButton(); // daemon is up now — make sure the dev button is shown
}
waitForReady();
