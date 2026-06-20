# Jack — floating orb shell (Tauri)

A thin [Tauri v2](https://tauri.app) window that loads the live orb client
(`../orb/index.html`) and makes it float on screen as Jack's always-available
presence. It carries **no logic** — it just renders state streamed from the
Python daemon over the localhost WebSocket.

```
ui/
  orb/              the web client (also opens standalone in a browser)
    index.html
  orb-shell/        this Tauri wrapper
    src-tauri/
      tauri.conf.json   window config (borderless, transparent, always-on-top)
      Cargo.toml
      build.rs
      src/main.rs        macOS accessory policy (no Dock icon)
      capabilities/      window permissions the JS uses
```

## Prerequisites (one time, on macOS)

1. **Rust**: <https://rustup.rs> → `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
2. **Xcode command-line tools**: `xcode-select --install`
3. **Tauri CLI**: `cargo install tauri-cli --version "^2"`
4. **Icons**: a source icon ships at `src-tauri/app-icon.png` (the Jack orb).
   Generate the platform icon set once, from `src-tauri/`:

   ```bash
   cargo tauri icon app-icon.png      # or just `cargo tauri icon` (defaults to app-icon.png)
   ```

   This writes `src-tauri/icons/` (gitignored, regenerated on demand). To
   rebrand, replace `app-icon.png` with your own square PNG (≥1024×1024) and
   re-run.

## Run it

In one terminal, start the engine + daemon (from the repo root):

```bash
python -m autobot.daemon          # real engine; or `--demo` to cycle states
```

In another, launch the orb window (from `ui/orb-shell/src-tauri/`):

```bash
cargo tauri dev
```

You should get a borderless, transparent orb floating top-right, on top of every
app and Space, with no Dock icon. It connects to `ws://127.0.0.1:8765/ws` and
reacts to Jack's state.

## Controls (menu-bar icon)

Jack lives in the macOS menu bar (no Dock icon). Its menu has two state-aware
toggles, a Size submenu, and Quit:

- **Show orb / Hide orb** (one item that flips): hide it entirely, bring it back
  any time. The label always shows what the click will do.
- **Movable / Locked** (one item that flips; its label is the current mode):
  - *Movable*: the window takes the mouse, so you can drag the orb anywhere
    (position is remembered across launches).
  - *Locked*: the orb ignores the mouse and becomes pure ambiance — clicks pass
    straight through to whatever's underneath. Day-to-day you'll want it locked;
    switch to movable only to reposition.
- **Size ▸ Small / Medium / Large**: resize the orb; the choice is remembered
  across launches.
- **Settings…**: opens a window to choose the model/provider (Local Ollama or
  cloud Claude), enter an API key (stored in the Keychain), and toggle
  capabilities. It talks to the daemon's settings API, so **the daemon must be
  running**; changes apply on restart.
- **Quit Jack**.

The orb floats **above other app windows, across every Space, and over
full-screen apps** — so when Jack speaks you actually see it on whatever you're
looking at, not just hear it.

A plain `NSWindow` cannot be drawn over another app's full-screen Space at *any*
window level (verified). The working recipe — used by Spotlight-style overlays —
is to convert the window into a **non-activating `NSPanel`**. `make_floating_panel()`
in `src/main.rs` uses the [`tauri-nspanel`](https://github.com/ahkohd/tauri-nspanel)
plugin to `to_panel()` the orb, then sets the non-activating style mask,
`canJoinAllSpaces | fullScreenAuxiliary | stationary`, and a level just above the
menu bar. Non-activating means showing the orb never steals focus from your editor.

> macOS gotcha: this behaves differently between `cargo tauri dev` and a packaged
> `cargo tauri build` — verify both.

## "Come to me" on wake

By default the orb rests wherever you've parked it. The moment you address Jack
(it enters *thinking* / *talking*), it **glides to the centre-top of the screen
you're working on**, then slides back to its parked spot when the turn ends — so
it feels like it comes over to help and tidies itself away. Implemented in
`../orb/index.html` (it reacts to the daemon's state stream and moves its own
window via `currentMonitor` + `setPosition`).

- Disable it: in the orb's webview, set `localStorage.jackComeForward = "0"`
  (a tray toggle for this is an easy follow-up).
- It targets the display the orb is on. Pinpointing the *exact focused window*
  of another app needs macOS Accessibility APIs — a larger, permission-gated
  feature we can add later if you want true per-window tracking.

Build a release app bundle:

```bash
cargo tauri build      # produces Jack.app
```

## What the window does (and where it's set)

| Behavior | Where |
|----------|-------|
| Borderless, transparent, always-on-top, off-taskbar, fixed size | `tauri.conf.json` → `app.windows` |
| Transparent webview on macOS | `tauri.conf.json` → `app.macOSPrivateApi: true` |
| No Dock icon / not in ⌘-Tab (accessory) | `src/main.rs` (`set_activation_policy`) |
| Visible on active Space + over full-screen apps | `src/main.rs::make_floating_panel()` (non-activating NSPanel via `tauri-nspanel`) |
| Menu-bar tray: Show/Hide + Movable/Locked toggles, Size submenu, Quit | `src/main.rs` (tray menu, state-aware labels) |
| Click-through toggle | `src/main.rs` (`set_ignore_cursor_events`) |
| Size presets (Size submenu) | `src/main.rs` (`set_size`) |
| Draggable by grabbing the orb (when unlocked) | `../orb/index.html` Tauri init (`startDragging`) |
| Remembers its position and size across launches | `../orb/index.html` Tauri init (`onMoved`/`onResized` → `localStorage`) |
| Allowed to talk to the daemon socket | `tauri.conf.json` → `app.security.csp` |

## Not yet wired (next iterations)

- **Exact focused-window placement (Step 2, in progress)**: on wake, land the orb
  precisely over the focused app window (e.g. your editor) via the macOS
  Accessibility API, with a fallback to the active display when permission isn't
  granted. Today's behavior makes it *visible* over your current Space; this adds
  precise *placement*.
- **Acoustic wake word** so the orb shows a true "listening" glow the instant you
  say the wake word (engine-side; needs a wake model — see the roadmap).
- **Multi-monitor clamp**: if a saved position lands off a now-disconnected
  display, snap it back on-screen.
- **Settings** in the tray (daemon URL, color) if it grows beyond lock/quit.

## Note

This scaffold was authored without a local Rust build available, so the first
`cargo tauri dev` may surface a version-specific tweak (an API name or a
permission identifier). The structure and config are standard Tauri v2; fixes are
expected to be one-liners. If `set_activation_policy` doesn't compile on your
Tauri version, comment that line in `src/main.rs` (the orb still floats; it just
keeps a Dock icon).
