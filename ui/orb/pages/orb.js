/** Jack — live orb client. Renders the four-state orb (lib/orb-renderer) and the
 *  notification cards (orb-cards), driven by the daemon WebSocket. Owns the Tauri
 *  window glue (auto-hide, come-to-me glide, position/size persistence). Moved from
 *  index.html's IIFE; rendering + card DOM now live in their own modules. */
import { daemon } from "../lib/daemon.js";
import { createOrbRenderer, SC } from "../lib/orb-renderer.js";
import { createEarcons } from "../lib/earcons.js";
import { $ } from "../lib/dom.js";
import { mcpInfoForTool } from "../components/chat-log/chat-log.js";
import "../components/orb-cards/orb-cards.js";

const conn = $("conn");
const orbCards = $("cards"); // the <orb-cards id="cards"> element

// MCP server map cache for egress ring
let _serverMap = {};
let _ringFadeTimeout = null;

const earcons = createEarcons({ gain: 0.30 });
// Some webviews start the audio context suspended until a gesture; resume once.
earcons.resumeOnGesture();
// Mode-switch cue, invoked by the native shell (close_chat) when flipping chat<->voice.
window.__modeEarcon = (mode) => earcons.playMode(mode);

const renderer = createOrbRenderer($("gl"), $("ov"));
if (!renderer) {
  conn.textContent = "WebGL not available";
} else {
  init();
}

function init() {
  // --- Tauri window state (shared with sizing + glide logic) -----------------
  let tauriApi = null, tauriWin = null, suppressSave = false, parkedPos = null, animTok = 0;
  const ENGAGED = { listening: 1, thinking: 1, talking: 1 };

  // --- window sizing for cards (Tauri) ---------------------------------------
  const CARD_W = 340; let _baseSize = null;
  function enterCardMode() { document.body.classList.add("has-card"); sizeWindowForCards(); }
  function exitCardMode() { document.body.classList.remove("has-card"); restoreSize(); }
  function sizeWindowForCards() {
    if (!tauriWin || !tauriApi || !tauriApi.PhysicalSize) return;
    const dprNow = Math.min(window.devicePixelRatio || 1, 2);
    tauriWin.outerSize().then((sz) => {
      if (!_baseSize) _baseSize = { w: sz.width, h: sz.height };
      const w = Math.round(CARD_W * dprNow);
      suppressSave = true;
      tauriWin.setSize(new tauriApi.PhysicalSize(w, sz.height)); // widen first, let it reflow
      requestAnimationFrame(() => {
        const h = Math.round((document.body.scrollHeight + 8) * dprNow);
        tauriWin.setSize(new tauriApi.PhysicalSize(w, h));
        try { ensureOnScreen(); } catch (e) {} // nudge back if widening ran off the edge
        setTimeout(() => { suppressSave = false; }, 200);
      });
    }).catch(() => {});
  }
  function restoreSize() {
    if (!tauriWin || !tauriApi || !tauriApi.PhysicalSize || !_baseSize) { _baseSize = null; return; }
    suppressSave = true;
    try { tauriWin.setSize(new tauriApi.PhysicalSize(_baseSize.w, _baseSize.h)); } catch (e) {}
    try { ensureOnScreen(); } catch (e) {}
    _baseSize = null;
    setTimeout(() => { suppressSave = false; }, 200);
  }
  // Wire the card component's hooks to the window-sizing + show-orb glue.
  orbCards.enterCardMode = enterCardMode;
  orbCards.exitCardMode = exitCardMode;
  orbCards.ensureOrb = () => { try { showOrb(); } catch (e) {} };

  // --- "come to me": glide to the active screen when engaged (Tauri only) ----
  function comeForwardEnabled() { try { return localStorage.getItem("jackComeForward") === "1"; } catch (e) { return false; } }

  function onStateChange(prev, next) {
    earcons.playState(next); // audible status cue, alongside the orb's color/animation
    // A new turn began: drop any search preview from the previous turn.
    if (prev === "idle" && (next === "listening" || next === "thinking")) orbCards.clearChoices();
    if (!tauriWin) return;
    if (next !== "idle") showOrb();
    else if (pendingHide) hideOrb();
    else scheduleIdleHide();
    if (comeForwardEnabled()) {
      const was = ENGAGED[prev], now = ENGAGED[next];
      if (!was && now) comeForward();
      else if (was && !now) goBack();
    }
  }

  // --- auto-hide on idle + dismiss-by-voice (Tauri only) ---------------------
  const IDLE_HIDE_MS = 30000; let idleTimer = null, orbHidden = false, pendingHide = false;
  function autoHideEnabled() { try { return localStorage.getItem("jackAutoHide") !== "0"; } catch (e) { return true; } }
  function hideOrb() {
    pendingHide = false; clearTimeout(idleTimer); idleTimer = null;
    if (!tauriWin || orbHidden) return;
    orbHidden = true; if (tauriWin.hide) tauriWin.hide();
  }
  function ensureOnScreen() {
    if (!tauriWin || !tauriApi || !tauriApi.PhysicalPosition || !tauriApi.availableMonitors || !tauriApi.primaryMonitor) return;
    Promise.all([tauriWin.outerPosition(), tauriWin.outerSize(), tauriApi.availableMonitors()]).then((r) => {
      const pos = r[0], size = r[1], mons = r[2] || [];
      if (!mons.length) return;
      const fits = mons.some((m) => {
        const mp = m.position, ms = m.size;
        return pos.x >= mp.x - 4 && pos.y >= mp.y - 4 &&
          pos.x + size.width <= mp.x + ms.width + 4 && pos.y + size.height <= mp.y + ms.height + 4;
      });
      if (fits) return;
      tauriApi.primaryMonitor().then((m) => {
        if (!m) return;
        const sc = m.scaleFactor || 1;
        const x = m.position.x + Math.max(0, m.size.width - size.width - Math.round(20 * sc));
        const y = m.position.y + Math.round(56 * sc);
        tauriWin.setPosition(new tauriApi.PhysicalPosition(x, y));
      }).catch(() => {});
    }).catch(() => {});
  }
  function showOrb() {
    clearTimeout(idleTimer); idleTimer = null;
    if (!tauriWin) return;
    orbHidden = false;
    if (tauriWin.show) tauriWin.show();
    ensureOnScreen(); // a stale/off-screen saved position would make it invisible
    // Do NOT re-set always-on-top / visible-on-all-workspaces here (NSPanel config is
    // done once natively; re-setting resets the panel's collection behavior).
  }
  function scheduleIdleHide() {
    clearTimeout(idleTimer); idleTimer = null;
    if (!tauriWin || !autoHideEnabled()) return;
    idleTimer = setTimeout(hideOrb, IDLE_HIDE_MS);
  }
  function requestHide() {
    if (!tauriWin) return;
    if (renderer.state === "idle") hideOrb(); else pendingHide = true; // wait for the goodbye
  }
  function glideTo(tx, ty) {
    if (!tauriWin || !tauriApi || !tauriApi.PhysicalPosition) return;
    const P = tauriApi.PhysicalPosition, tok = ++animTok;
    tauriWin.outerPosition().then((cur) => {
      const sx = cur.x, sy = cur.y, steps = 18; let i = 0; suppressSave = true;
      (function step() {
        if (tok !== animTok) return;
        i++; const e = i / steps, k = 1 - Math.pow(1 - e, 3);
        tauriWin.setPosition(new P(Math.round(sx + (tx - sx) * k), Math.round(sy + (ty - sy) * k)));
        if (i >= steps) { setTimeout(() => { if (tok === animTok) suppressSave = false; }, 150); return; }
        setTimeout(step, 16);
      })();
    }).catch(() => {});
  }
  function comeForward() {
    if (!tauriApi.currentMonitor) return;
    Promise.all([tauriWin.outerPosition(), tauriApi.currentMonitor(), tauriWin.outerSize()]).then((r) => {
      if (!parkedPos) parkedPos = r[0];
      const mon = r[1], sz = r[2]; if (!mon) return;
      const tx = mon.position.x + Math.round((mon.size.width - sz.width) / 2);
      const ty = mon.position.y + Math.round(mon.size.height * 0.18);
      glideTo(tx, ty);
    }).catch((e) => { console.warn("comeForward", e); });
  }
  function goBack() {
    if (!parkedPos) return;
    const p = parkedPos; parkedPos = null; glideTo(p.x, p.y);
  }

  // --- MCP egress ring -------------------------------------------------------
  async function refreshServerMap() {
    try {
      const res = await daemon.mcpServers();
      if (res && res.servers) {
        _serverMap = {};
        res.servers.forEach((srv) => {
          _serverMap[srv.server] = {
            label: srv.label,
            icon: srv.icon || srv.label?.[0].toUpperCase() || "🔌",
            egress: srv.egress,
          };
        });
      }
    } catch (e) {
      // On fetch failure, stay graceful: _serverMap stays empty → no ring
    }
  }

  function activateEgressRing(label) {
    const ring = $("net-ring");
    const conn = $("net-conn");
    const cap = $("net-cap");
    if (ring && conn && cap) {
      ring.classList.add("active");
      conn.classList.add("active");
      cap.textContent = "Reaching " + label + "…";
      cap.classList.add("active");
    }
    // Clear any pending fade timeout
    if (_ringFadeTimeout) clearTimeout(_ringFadeTimeout);
    _ringFadeTimeout = null;
  }

  function deactivateEgressRing() {
    const ring = $("net-ring");
    const conn = $("net-conn");
    const cap = $("net-cap");
    if (ring && conn && cap) {
      ring.classList.remove("active");
      conn.classList.remove("active");
      cap.classList.remove("active");
    }
    // Clear any pending fade timeout
    if (_ringFadeTimeout) clearTimeout(_ringFadeTimeout);
    _ringFadeTimeout = null;
  }

  // --- daemon connection ------------------------------------------------------
  let prevState = "idle";
  daemon.on("state", (msg) => {
    if (!SC[msg.value]) return;
    const prev = prevState;
    if (prev === msg.value) return;
    prevState = msg.value; renderer.setState(msg.value); onStateChange(prev, msg.value);
  });
  daemon.on("amplitude", (msg) => renderer.setAmplitude(msg.value));
  daemon.on("visibility", (msg) => { if (msg.value === "hide") requestHide(); else showOrb(); });
  daemon.on("confirm", (msg) => { if (msg.mode !== "chat") { orbCards.clearChoices(); orbCards.showConfirm(msg.text, msg.kind); } });
  daemon.on("confirm_clear", () => orbCards.clear());
  daemon.on("choices", (msg) => { if (msg.mode === "voice") orbCards.showChoices(msg); });
  daemon.on("step", (m) => {
    const info = mcpInfoForTool(m.tool, _serverMap);
    if (m.status === "running" && info && info.egress) {
      activateEgressRing(info.label);
    } else if ((m.status === "done" || m.status === "failed") && info && info.egress) {
      // Remove active class immediately; CSS transition handles the visual fade-out.
      deactivateEgressRing();
    }
  });
  daemon.on("mcp_status", () => refreshServerMap());
  daemon.onOpen(() => { conn.textContent = ""; refreshServerMap(); });
  daemon.onClose(() => { conn.textContent = "reconnecting…"; });

  // The orb can launch hidden (chat-first); re-run resize whenever the canvas gains
  // size (i.e. when surfaced), since a window 'resize' won't fire then.
  const glCanvas = $("gl");
  window.addEventListener("resize", () => renderer.resize());
  if (window.ResizeObserver) { try { new ResizeObserver(() => renderer.resize()).observe(glCanvas); } catch (e) {} }
  conn.textContent = "connecting…";
  renderer.start();
  daemon.connect();

  // --- Tauri shell integration (no-op in a plain browser) --------------------
  if (window.__TAURI__ && window.__TAURI__.window) {
    try {
      document.body.style.background = "transparent";
      if (conn) conn.style.display = "none";
      const api = window.__TAURI__.window;
      const win = (api.getCurrentWindow || api.getCurrent).call(api);
      tauriApi = api; tauriWin = win; // shared with the glide/sizing logic
      window.__showOrb = showOrb; // let the shell (close_chat / ⌘⌃V) re-show + resync JS state
      if (win.setAlwaysOnTop) win.setAlwaysOnTop(true);
      if (win.setVisibleOnAllWorkspaces) win.setVisibleOnAllWorkspaces(true);
      const stage = document.querySelector(".stage");
      if (stage) stage.addEventListener("mousedown", () => { if (win.startDragging) win.startDragging(); });

      // Remember where the user parked the orb (persists via the webview's localStorage).
      const POS_KEY = "jackOrbPos", PhysPos = api.PhysicalPosition;
      try {
        const saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
        if (saved && PhysPos && typeof saved.x === "number") win.setPosition(new PhysPos(saved.x, saved.y));
      } catch (e) {}
      ensureOnScreen(); // clamp onto a real monitor if the saved/default pos is off-screen
      if (win.onMoved) win.onMoved((ev) => {
        if (suppressSave) return; // ignore programmatic glides — keep the parked spot
        const p = ev && ev.payload ? ev.payload : ev;
        if (p && typeof p.x === "number") localStorage.setItem(POS_KEY, JSON.stringify({ x: p.x, y: p.y }));
      });

      // Remember the chosen size too (set from the tray's Small/Medium/Large).
      const SIZE_KEY = "jackOrbSize", PhysSize = api.PhysicalSize;
      try {
        const sz = JSON.parse(localStorage.getItem(SIZE_KEY) || "null");
        if (sz && PhysSize && typeof sz.w === "number") win.setSize(new PhysSize(sz.w, sz.h));
      } catch (e) {}
      if (win.onResized) win.onResized((ev) => {
        const s = ev && ev.payload ? ev.payload : ev;
        if (s && typeof s.width === "number") localStorage.setItem(SIZE_KEY, JSON.stringify({ w: s.width, h: s.height }));
      });

      // Fresh launch: only arm auto-hide if the orb is actually visible (chat-first
      // launches it hidden; arming then would fight close_chat on the switch to voice).
      if (win.isVisible) {
        win.isVisible().then((v) => { if (v) scheduleIdleHide(); }).catch(() => {});
      } else {
        scheduleIdleHide();
      }
    } catch (e) { console.warn("tauri init failed", e); }
  }
}
