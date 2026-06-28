# System toggles — change my Mac by voice/chat (design)

Design reference for **GitHub issue #4 — "System toggles (volume / brightness /
dark mode / DND / sleep)"** (Track 1, capability growth). Records *how* the
write-side system controls work after this change and *why* they're built this
way. Status/tracking lives in the issue, not here.

> Goal in one line: give Jack a small family of **WRITE** tools to change common
> macOS settings — volume, brightness, appearance (dark/light), and sleep —
> entirely on-device, routed through the registry + permission gate like every
> other tool, so "turn it down", "go dark", "set brightness to 40", and "go to
> sleep" just work by voice or chat.

## 1. Context — the problem

[`tools/system.py`](../../src/autobot/tools/system.py) is the **read-only** half
of "system" — battery / Wi-Fi / disk, each a `READ_ONLY` query that answers
*"how's my Mac?"*. Issue #4 is its missing **write** half: *"change my Mac."* It's
the natural next step toward the product vision — control the whole Mac by simple
chat or voice, privacy by design (nothing leaves the machine).

The five settings the issue lists are **not equally feasible on-device** on
current macOS (Sequoia, the dev tier). Three are clean; two are fragile (see §6):

| Setting | On-device mechanism | Verdict |
|---|---|---|
| Volume | `osascript` `set volume …` (Standard Additions; reads current too) | ✅ Solid |
| Dark mode | `osascript` System Events appearance prefs | ✅ Solid |
| Sleep | `pmset sleepnow` | ✅ Solid |
| Brightness | **No native CLI** — optional `brightness` binary, else AppleScript key-codes | ⚠️ Needs graceful degradation |
| Do Not Disturb / Focus | **No reliable native CLI** post-Big Sur; needs a Shortcuts bridge + one-time user setup | ❌ Deferred to its own issue |

So this change ships the **reliable core** — volume, dark mode, sleep, and
brightness (with graceful degradation) — and **defers DND** to a dedicated
follow-up issue where the Shortcuts-bridge design gets the room it needs.

## 2. Goals / non-goals

**Goals**

1. A new `tools/toggles.py` — the write-side sibling of `system.py` — exposing
   `set_volume`, `set_brightness`, `set_appearance`, and `sleep_mac`.
2. Every tool is `WRITE`, audited, and flows through the permission gate, with no
   confirmation prompt (each action is instantly reversible).
3. Brightness degrades gracefully: precise % via the optional `brightness` binary,
   else relative up/down via AppleScript, else a friendly "here's how" message —
   **no hard dependency**, mirroring how STT falls back whisper.cpp → faster-whisper.
4. Pure, injected-`Runner` logic so the whole module is unit-tested with canned
   command output — no real audio, display, or sleep during tests.
5. A new `allow_system_toggles: bool = True` config flag, wired in `app.py::build()`.

**Non-goals (tracked separately — see §8)**

- **Do Not Disturb / Focus** — its own follow-up issue (Shortcuts bridge).
- **Broader "system control" family** — Wi-Fi on/off, keep-awake (`caffeinate`),
  lock screen. Reliable and on-theme, but out of scope for #4.
- Reading-back tools (`get_volume` etc.) as separate tools — the write tools
  report the resulting state in their reply; a standalone reader isn't needed yet.

## 3. Approach

**Approach A — a new `toggles.py` sibling module** (chosen). Mirror `system.py`
exactly: a `SystemToggles` class with an injected `Runner`, pure parse/format
helpers, a `specs()` list, and a `register_system_toggles()` function, behind its
own `allow_system_toggles` flag. Keeps the read (`system.py`) and write
(`toggles.py`) responsibilities — and their config flags — cleanly separated.

> Rejected: **B** — bolting write methods onto `SystemTools` mixes `READ_ONLY`
> status with `WRITE` actions under one class and the `allow_system_info` flag
> ("system info" gating writes is semantically wrong). **C** — one file per
> toggle, which over-fragments (system.py already groups related tools).

### 3.1 The four tools

| Tool | Params | Mechanism | Risk | `requires` | `ack` |
|---|---|---|---|---|---|
| `set_volume` | `level` 0–100 and/or `action`: mute \| unmute \| up \| down | `osascript` `set volume …`; reads current via `get volume settings` for up/down | `WRITE` | none | "Adjusting the volume." |
| `set_brightness` | `level` 0–100 and/or `action`: up \| down | `brightness` binary (absolute) → AppleScript key-codes 144/145 (relative) → setup message | `WRITE` | none (see §3.3) | "Adjusting brightness." |
| `set_appearance` | `mode`: dark \| light \| toggle | `osascript` System Events `set dark mode …` | `WRITE` | **automation** | "Switching the appearance." |
| `sleep_mac` | none | `pmset sleepnow` | `WRITE` | none | "Going to sleep." |

**Tool shape decision:** `set_volume`/`set_brightness` each take an optional
`level` *and* an optional `action` (absolute + relative/mute in one tool, the
`ToolSpec.description` steering which to use), rather than splitting mute/relative
into separate tools. One tool per domain keeps the tool surface small (already a
concern at ~40 tools) and matches how a person speaks about "the volume."

### 3.2 Command details (verified — see §6)

```text
# Volume (Standard Additions — NOT an app target, so no Automation permission)
read current : osascript -e 'output volume of (get volume settings)'      # 0..100
read muted   : osascript -e 'output muted of (get volume settings)'       # true|false
set absolute : osascript -e 'set volume output volume 30'                  # clamps 0..100
mute/unmute  : osascript -e 'set volume output muted true|false'
up/down      : read current, ±STEP (10), clamp 0..100, set absolute

# Appearance (System Events target → requires Automation)
toggle : osascript -e 'tell app "System Events" to tell appearance preferences to set dark mode to not dark mode'
force  : ... set dark mode to true   (dark)  /  false (light)     # idempotent, no restart
read   : ... return dark mode                                     # true|false

# Sleep (a binary, no app target, no permission)
pmset sleepnow

# Brightness (no native CLI)
absolute : brightness 0.4            # the Homebrew binary takes 0.0..1.0; level/100
relative : osascript -e 'tell app "System Events" to key code 144'  # 144 up / 145 down
```

Handlers parse with pure helpers (e.g. `parse_volume_settings(out) -> int`,
`clamp(level)`), exactly like `parse_battery` / `parse_disk` in `system.py`, so
the parsing is unit-tested against canned strings.

### 3.3 Brightness graceful degradation (the one nuance)

`set_brightness` tries, in order:

1. **Absolute `level` + `brightness` binary present** → `brightness <level/100>`.
   Best UX, no macOS permission. Detected by running it; `rc == 127` (command not
   found) means absent.
2. **`action` up/down** → AppleScript key-codes 144/145 via System Events. Sends
   the keycode (≈ one notch); needs **Accessibility** at runtime.
3. **Absolute `level` requested but binary absent** → friendly message: *"I can
   nudge brightness up or down. For an exact level, install the brightness tool:
   `brew install brightness`."*
4. **AppleScript path blocked** (Accessibility not granted) → detect the error and
   return a friendly *"…enable Accessibility for Jack in System Settings"* note.

**Why `requires` is left unset on brightness:** the field is static per
`ToolSpec`, but brightness has *two* paths with *different* permission needs — the
binary path needs none, the AppleScript path needs Accessibility. A static
`requires=accessibility` would make the gate block the tool (and pop Settings)
even when the binary is available and no permission is needed. So brightness
omits `requires` and handles a missing-permission outcome **inside the handler**
as a friendly string. (`set_appearance` keeps `requires=automation` — it has a
single path that always targets System Events.)

### 3.4 Wiring

- **`config.py`**: add `allow_system_toggles: bool = True` (on-device → defaults
  on, matching every other local capability; only off-device `allow_web` is off).
- **`app.py::build()`**: after the `allow_system_info` block —
  ```python
  if settings.allow_system_toggles:
      from autobot.tools.toggles import register_system_toggles
      register_system_toggles(registry)
      log.info("system toggles ENABLED (volume/brightness/appearance/sleep)")
  ```
- **`permissions.py`**: import `AUTOMATION` for `set_appearance.requires`.
- **Logging**: a `get_logger("toggles")` logger; INFO at each seam ("volume set
  to=%d", "appearance mode=%s", "sleeping"), per the logging rules.

## 4. Decisions (locked)

- **Scope:** volume, brightness, appearance (dark/light), sleep. **DND deferred**
  to a new follow-up issue (opened as part of this work, linked from #4).
- **Architecture:** Approach A — new `tools/toggles.py` sibling of `system.py`.
- **Risk:** all four `WRITE` → audited, **no confirmation** (all reversible; a
  voice "turn it up" must not prompt). Gate confirms only `DESTRUCTIVE`.
- **Brightness:** graceful degradation (binary → AppleScript relative → message);
  no hard dependency; `requires` left unset, permission handled at runtime.
- **Tool shape:** one tool per domain; `set_volume`/`set_brightness` take optional
  `level` + optional `action`.
- **Config:** `allow_system_toggles: bool = True`.

## 5. Testing

New `tests/unit/test_toggles.py`, fully offline via a `FakeRunner` (mirrors
`test_system.py` / `test_trash.py`):

- **Pure helpers:** `parse_volume_settings`, clamp (e.g. 130 → 100, -5 → 0),
  appearance read parsing.
- **set_volume:** absolute calls `osascript` with the right level; up/down read
  current then set current±step (clamped at the ends); mute/unmute set
  `output muted`; out-of-range and "no args" return friendly strings.
- **set_brightness:** binary present → calls `brightness level/100`; binary absent
  (`rc 127`) + absolute → setup message; up/down → key-code 144/145; Accessibility
  error → friendly note.
- **set_appearance:** dark/light force `true`/`false`; toggle uses `not dark mode`;
  reports the resulting mode.
- **sleep_mac:** calls `pmset sleepnow`; non-zero rc → friendly failure.
- **Registration/specs:** all four registered, all `Risk.WRITE`,
  `set_appearance.requires == AUTOMATION`, others `None`; no confirmation needed.
- `make check` (ruff, ruff-format, mypy strict, pytest) stays green.

## 6. Research & prior art (cited)

A web review of what's controllable on-device on macOS Sequoia, and how reliable
each path is. Summary: **volume / dark mode / sleep are first-class and stable;
brightness has no native CLI; DND lost its CLI after Big Sur.**

**Volume — solid, no permission.** `set volume output volume N` (0–100),
`set volume output muted true|false`, and reading via `get volume settings` are
Standard Additions commands (not app-targeted), so no Automation prompt.
Refs: [Control OS X volume with AppleScript](https://coolaj86.com/articles/how-to-control-os-x-system-volume-with-applescript/),
[osascript / ss64](https://ss64.com/mac/osascript.html).

**Dark mode — solid, needs Automation.** `tell app "System Events" to tell
appearance preferences to set dark mode to not dark mode` toggles; `set dark mode
to true|false` forces. Idempotent, no daemon kick, works Mojave → current.
Targeting System Events means the controlling app needs Automation permission.
Ref: [Toggle macOS Dark Mode from the command line](https://techearl.com/mac-dark-mode-command-line).

**Sleep — solid.** `pmset sleepnow` (a binary, no app target, no permission).
Reversible — waking the Mac restores everything; hence `WRITE`, not destructive.
Ref: [osascript / ss64](https://ss64.com/mac/osascript.html).

**Brightness — no native CLI (the fragile one).** Two options: the Homebrew
[`brightness`](https://github.com/nriley/brightness) binary gives precise absolute
control (`0.0–1.0`) but is an external install; or AppleScript key-codes 144/145
nudge one notch at a time and require Accessibility. Both work on Sequoia; neither
is built in. Hence graceful degradation (§3.3).
Refs: [nriley/brightness](https://github.com/nriley/brightness),
[Adjust screen brightness from the command line](https://osxdaily.com/2019/08/14/change-screen-brightness-mac-terminal/).

**Do Not Disturb / Focus — no reliable native CLI (deferred).** The old
`defaults write com.apple.ncprefs …` hack broke after Big Sur; on Ventura+/Sequoia
the robust path is **Shortcuts** — create a "Set Focus" shortcut once, then
`shortcuts run "<name>"`. That one-time setup and bridge design is its own issue.
Refs: [Turn on Focus mode from the Terminal](https://heyfocus.com/blog/how-to-turn-on-mac-focus-mode-from-the-terminal/),
[Apple — Turn a Focus on or off on Mac](https://support.apple.com/guide/mac-help/turn-a-focus-on-or-off-mchl999b7c1a/mac).

## 7. Risks

- **Brightness portability** — the `brightness` binary and AppleScript key-codes
  vary across Apple Silicon and external displays. Mitigated by graceful
  degradation + always returning a friendly, actionable message (never a raw
  failure).
- **AppleScript permission friction** — first `set_appearance` (and brightness
  AppleScript fallback) can trip the Automation/Accessibility prompts. The gate
  already opens the right Settings pane for `requires`; brightness explains itself
  at runtime. Documented behavior, not a bug.
- **Sleep is disruptive** — it blanks the screen immediately. Accepted: it's only
  invoked on explicit request, fully reversible, and a confirmation prompt on
  "go to sleep" would be worse UX than the action.

## 8. Future work (separate issues)

- **Do Not Disturb / Focus** — new follow-up issue. Shortcuts bridge:
  `shortcuts run`, detect whether the needed shortcut exists (`shortcuts list`),
  and guide a one-time setup when it doesn't. Privacy-clean (Shortcuts runs
  locally).
- **Broader system-control family** — Wi-Fi on/off (`networksetup
  -setairportpower`, pairs with the existing Wi-Fi *status* tool), keep-awake
  (`caffeinate`, the friendly inverse of sleep), lock screen. Reliable + native;
  bundle into a future "more system controls" issue if wanted.
