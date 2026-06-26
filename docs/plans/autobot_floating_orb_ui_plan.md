# Jack — Floating Orb UI Plan

> **Status: SHIPPED (v0.4.0).** The floating orb and the right-docked chat drawer
> are live in `ui/orb-shell` (Tauri shell) + `ui/orb/index.html` (WebGL orb) and
> `ui/orb/chat.html` (chat). This doc is kept as the design record; the build steps
> below are done.

Jack's always-available visual presence: a floating energy-orb that lives on screen
across every app and Space, driven by the headless daemon (the orb is a thin client
of it).

Design reference: `docs/ui/jack_orb_prototype.html`.

---

## Non-negotiables carried in

- **On-device only.** The orb is a local webview talking to a localhost socket.
  No audio, text, or telemetry leaves the machine. (Same rule as the rest of the
  project; the only sanctioned off-device call remains opt-in `web_search`.)
- **Engine stays headless.** The orb is a *thin client*. All logic — STT, LLM,
  TTS, permission gate — stays in the Python daemon. The UI only renders state
  and forwards user intent (e.g. "summon"). Never move logic into the UI.
- **No terminal in the user's face.** The user sees only the orb. The terminal
  client (Textual) remains a developer/debug surface, not the product.

---

## The locked look

A WebGL energy-orb: turbulent procedural plasma + electric veins + soft outer
glow, with the bright rim dialed down (`rim ≈ 0.04`). It is **alive** — always
moving, never a static icon — which is what makes it read as autonomous.

Four states, each instantly distinguishable by **colour + a signature motion**
(colour does the across-the-room recognition; the motion cue confirms it):

| State | Colour | Motion cue | Driven by (production) |
|-------|--------|-----------|------------------------|
| Idle | Blue | Slow lazy swirl, faint breathing halo | — |
| Listening | Cyan | Ripples radiating outward | **live mic amplitude** |
| Thinking | Purple | Spinner orbiting the orb | — (steady) |
| Talking | Gold | Waveform ring pulsing in bursts | **TTS audio envelope** |

Transitions are smoothly interpolated (colour/speed/energy lerp), never snapped.
In the prototype, Listening/Talking pulses are simulated; in production they come
from real amplitude over the socket (see contract).

Open to later tuning without re-architecting: which colour maps to which state,
swapping a cue, orb size. These are constants in one place.

---

## Tech choice: Tauri (locked)

Per the design reference's "Desktop UI" row
(`docs/architecture/design-reference.md`) and the user's "fast and optimised, not
heavyweight" requirement:

- **Tauri** (Rust shell + system webview) — a few-MB binary and low RAM vs
  Electron, with the best native window control of the options (transparency,
  always-on-top, click-through, all-Spaces). The orb itself is the WebGL canvas
  from the reference file, rendered in the webview.
- The Rust side is thin: create/position the window, set the macOS window flags
  that aren't exposed by default, hold the WebSocket connection to the daemon,
  and forward `{state, amplitude}` frames to JS. No business logic.
- Rejected: **pywebview** (simplest to bolt onto Python, but weaker window
  control and heavier per-window cost); **native Swift** (best native feel but a
  whole separate codebase from the Python engine — too much drag for now).

> This is consistent with the "On Rust" note in the design reference: we are NOT rewriting the
> engine in Rust. Only the UI shell is Rust, because that's where the native
> window behaviour lives. Orchestration stays Python.

---

## The "always there, floats anywhere" window recipe (macOS)

The behaviours the user asked for, mapped to concrete window config:

- **Borderless + transparent** — `decorations: false`, `transparent: true`, no
  background; only the orb's pixels are visible, no frame or title bar.
- **Always-on-top** — `alwaysOnTop: true` so it sits above normal app windows.
- **Visible on every Space and over full-screen apps** — set the AppKit
  collection behaviour `canJoinAllSpaces | fullScreenAuxiliary` on the
  `NSWindow` (via a small Rust call using the window's native handle). This is
  what makes it "stick around no matter what app the user opens."
- **Not an app window** — accessory activation policy (`LSUIElement` /
  `NSApplicationActivationPolicyAccessory`) so it doesn't appear in the Dock or
  the ⌘-Tab switcher. It's a presence, not an app.
- **Draggable anywhere, position remembered** — drag to reposition; persist
  `(x, y)` locally and restore on launch. Snap to the nearest screen corner is a
  nice-to-have.
- **Click-through when idle** — `setIgnoreCursorEvents(true)` while idle so it
  never blocks what's underneath; capture clicks only when summoned/active (e.g.
  a small interaction zone, or toggle on wake).
- **Multi-monitor** — clamp the saved position to a currently-attached display so
  it never restores off-screen.

A companion **menu-bar item** is a small optional add (show/hide orb, quit,
settings) but is not required for v1 — the user chose the floating orb as the
primary surface.

---

## Daemon contract (Phase 3b prerequisite)

The orb needs the daemon to exist and to stream state. This is the Phase 3b work
("make the engine a headless daemon — FastAPI + localhost WebSocket") plus a thin
schema for the orb. Define it once; both the Textual client and the orb consume it.

Server → client (pushed as the turn progresses):

```jsonc
// state transition
{ "type": "state", "value": "idle|listening|thinking|talking" }
// amplitude frames (only meaningful in listening/talking), 0.0–1.0, ~30–60 Hz
{ "type": "amplitude", "value": 0.42 }
// optional: short caption / last transcript line for an expanded view (later)
{ "type": "caption", "value": "..." }
```

Client → server (user intent forwarded by the UI, never logic):

```jsonc
{ "type": "summon" }        // user clicked/tapped the orb to start a turn
{ "type": "cancel" }        // dismiss current turn
```

Notes:
- `state` maps 1:1 to the orchestrator's existing state machine
  (`idle → listening → transcribing/planning → executing/responding`). Collapse
  transcribing+planning into the orb's **thinking**, and responding/TTS into
  **talking**. The orchestrator already owns these transitions — we just emit them.
- `amplitude` for **listening** comes from the mic RMS the capture loop already
  computes (VAD path); for **talking** from the TTS output buffer's RMS. Both are
  already on-device; we only forward a normalized scalar.
- Keep the socket localhost-only; bind `127.0.0.1`, no external interface.

---

## Build steps (risk-ordered) — all complete

> These shipped as written (the Textual/CLI references below were the interim
> subscriber during bring-up; the product surface is the Tauri orb + chat drawer).

1. **Daemon skeleton (Phase 3b).** FastAPI + localhost WebSocket; emit `state`
   transitions from the orchestrator. Done when the existing Textual/CLI run can
   subscribe and print state changes live.
2. **Amplitude stream.** Forward mic RMS (listening) and TTS RMS (talking) as
   `amplitude` frames. Done when a CLI subscriber prints a moving number while
   you speak and while Jack speaks.
3. **Tauri shell + orb.** New `ui/orb/` Tauri app rendering the locked WebGL
   canvas; hardcode `state` cycling first to verify the visual in a real window.
   Done when the orb renders borderless/transparent in a normal window.
4. **Window behaviour.** Apply the macOS recipe above (always-on-top,
   all-Spaces, accessory policy, click-through, drag + persist). Done when the
   orb stays visible across app switches, Spaces, and a full-screen app, and
   never steals focus.
5. **Wire to daemon.** Replace the hardcoded cycle with live `{state, amplitude}`
   from the WebSocket; reconnect on drop. Done when speaking to Jack visibly
   drives idle→listening→thinking→talking with reactive pulses.
6. **Summon + polish.** Click-to-summon, position memory, multi-monitor clamp,
   optional menu-bar toggle. Done when the full hands-free loop is usable with
   the terminal fully hidden.

---

## Out of scope for v1 (note for later)

- Expanded panel (captions, last reply, quick actions) on hover/click.
- Tray/menu-bar control surface (toggle, settings, quit) — small follow-up.
- Notifications / proactive surfacing.
- Cross-platform window recipe (Windows/Linux) — design is portable, the window
  flags are macOS-specific and would need per-OS equivalents.

---

## Verification expectations

- Engine/daemon changes keep `make check` green (ruff, ruff-format, mypy strict,
  pytest); add tests for the new daemon message schema and any pure mapping
  (orchestrator state → orb state, RMS → normalized amplitude).
- The Tauri/JS orb is verified by eye against `docs/ui/jack_orb_prototype.html`
  and by a manual checklist for the window behaviour (stays over full-screen
  apps, no Dock icon, click-through when idle, position persists).
- Privacy check: confirm the socket is localhost-only and no new outbound calls
  are introduced.
