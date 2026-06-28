# System controls — change my Mac by voice/chat (design)

Design reference for **GitHub issue #4 — "System toggles (volume / brightness /
dark mode / DND / sleep)"** (Track 1, capability growth), **expanded** to a small
write-side *system-control* family. Records *how* these controls work after this
change and *why* they're built this way. Status/tracking lives in the issue, not
here.

> Goal in one line: give Jack a family of **WRITE** tools to change common macOS
> settings — volume, brightness, appearance (dark/light), sleep, Wi-Fi,
> keep-awake, and lock screen — entirely on-device, routed through the registry +
> permission gate like every other tool, so "turn it down", "go dark", "set
> brightness to 40", "turn off Wi-Fi", "keep my Mac awake for 30 minutes", "lock
> my screen", and "go to sleep" just work by voice or chat.

## 1. Context — the problem

[`tools/system.py`](../../src/autobot/tools/system.py) is the **read-only** half
of "system" — battery / Wi-Fi / disk, each a `READ_ONLY` query that answers
*"how's my Mac?"*. Issue #4 is its missing **write** half: *"change my Mac."* It's
the natural next step toward the product vision — control the whole Mac by simple
chat or voice, privacy by design (nothing leaves the machine).

The settings are **not equally feasible on-device** on current macOS (Sequoia,
the dev tier). Some are first-class; some are fragile and need graceful
degradation; one (DND) has no reliable path at all (see §6):

| Setting | On-device mechanism | Verdict |
|---|---|---|
| Volume | `osascript` `set volume …` (Standard Additions; reads current too) | ✅ Solid, no permission |
| Dark mode | `osascript` System Events appearance prefs | ✅ Solid, needs Automation |
| Sleep | `pmset sleepnow` | ✅ Solid, no permission |
| Keep-awake | `caffeinate` (long-running background process) | ✅ Solid, no permission |
| Brightness | **No native CLI** — optional `brightness` binary, else AppleScript key-codes | ⚠️ Graceful degradation |
| Wi-Fi on/off | `networksetup -setairportpower` | ⚠️ May need admin (policy-dependent) |
| Lock screen | `CGSession -suspend` (may be absent) → AppleScript Ctrl-Cmd-Q | ⚠️ Graceful degradation |
| Do Not Disturb / Focus | **No reliable native CLI** post-Big Sur; needs a Shortcuts bridge + one-time setup | ❌ Deferred to its own issue |

So this change ships the seven feasible controls (solid ones cleanly, the fragile
ones with honest graceful degradation) and **defers DND** to a dedicated
follow-up issue where the Shortcuts-bridge design gets the room it needs.

## 2. Goals / non-goals

**Goals**

1. A new `tools/toggles.py` — the write-side sibling of `system.py` — exposing
   `set_volume`, `set_brightness`, `set_appearance`, `sleep_mac`, `set_wifi`,
   `keep_awake`, and `lock_screen`.
2. Every tool is `WRITE`, audited, and flows through the permission gate, with no
   confirmation prompt (each action is instantly reversible).
3. The fragile tools degrade gracefully — they try the best path, then a fallback,
   then return a friendly, actionable message — **no hard dependency** and **no
   silent privilege escalation** (never `sudo`), mirroring how STT falls back
   whisper.cpp → faster-whisper.
4. Pure, injected logic so the whole module is unit-tested with canned command
   output and a fake process manager — no real audio, display, sleep, Wi-Fi
   toggling, or spawned processes during tests.
5. A new `allow_system_toggles: bool = True` config flag, wired in `app.py::build()`.

**Non-goals (tracked separately — see §8)**

- **Do Not Disturb / Focus** — its own follow-up issue (Shortcuts bridge).
- Standalone reader tools (`get_volume` etc.) — the write tools report the
  resulting state in their reply; a separate reader isn't needed yet.
- Bluetooth, Night Shift, True Tone, keyboard backlight — no clean on-device path
  (private frameworks / external `blueutil`); revisit only if asked.

## 3. Approach

**Approach A — a new `toggles.py` sibling module** (chosen). Mirror `system.py`
exactly: a `SystemToggles` class with an injected `Runner` (one-shot commands)
*and* an injected `ProcessManager` (for `caffeinate`'s background process), pure
parse/format helpers, a `specs()` list, and a `register_system_toggles()`
function, behind its own `allow_system_toggles` flag. Keeps the read
(`system.py`) and write (`toggles.py`) responsibilities — and their config flags —
cleanly separated.

> Rejected: **B** — bolting write methods onto `SystemTools` mixes `READ_ONLY`
> status with `WRITE` actions under one class and the `allow_system_info` flag
> ("system info" gating writes is semantically wrong). **C** — one file per
> control, which over-fragments (system.py already groups related tools).

### 3.1 The seven tools

| Tool | Params | Mechanism | Risk | `requires` | `ack` |
|---|---|---|---|---|---|
| `set_volume` | `level` 0–100 and/or `action`: mute \| unmute \| up \| down | `osascript` `set volume …`; reads current via `get volume settings` for up/down | `WRITE` | none | "Adjusting the volume." |
| `set_brightness` | `level` 0–100 and/or `action`: up \| down | `brightness` binary (absolute) → AppleScript key-codes 144/145 (relative) → setup message | `WRITE` | none (see §3.4) | "Adjusting brightness." |
| `set_appearance` | `mode`: dark \| light \| toggle | `osascript` System Events `set dark mode …` | `WRITE` | **automation** | "Switching the appearance." |
| `sleep_mac` | none | `pmset sleepnow` | `WRITE` | none | "Going to sleep." |
| `set_wifi` | `state`: on \| off \| toggle | `networksetup -setairportpower <dev>` (device resolved like system.py) | `WRITE` | none (see §3.4) | "Updating Wi-Fi." |
| `keep_awake` | `minutes` int (optional); `off` bool | `caffeinate -dimsu [-t N]` spawned + tracked; `off` kills it | `WRITE` | none | "Keeping your Mac awake." |
| `lock_screen` | none | `CGSession -suspend` → AppleScript Ctrl-Cmd-Q keystroke → message | `WRITE` | none (see §3.4) | "Locking the screen." |

**Tool shape decision:** `set_volume`/`set_brightness` each take an optional
`level` *and* an optional `action` (absolute + relative/mute in one tool, the
`ToolSpec.description` steering which to use), rather than splitting mute/relative
into separate tools. One tool per domain keeps the tool surface small (already a
concern at ~40 tools) and matches how a person speaks about "the volume." This
adds 7 tools; acceptable, but noted as a tool-surface pressure (§7).

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

# Wi-Fi (device resolved via networksetup -listallhardwareports, fallback en0)
read  : networksetup -getairportpower en0          # "Wi-Fi Power (en0): On"
set   : networksetup -setairportpower en0 on|off    # NO sudo; may fail if policy needs admin

# Lock screen (try native, fall back to keystroke)
native   : /System/Library/CoreServices/Menu\ Extras/User.menu/Contents/Resources/CGSession -suspend
fallback : osascript -e 'tell app "System Events" to keystroke "q" using {control down, command down}'

# Keep-awake (background process; -d display, -i idle, -m disk, -s system, -u user-active)
caffeinate -dimsu          # indefinite, until killed
caffeinate -dimsu -t 1800  # self-terminates after N seconds
```

Handlers parse with pure helpers (e.g. `parse_volume_settings(out) -> int`,
`clamp(level)`, and reusing `system.parse_wifi_device` for the Wi-Fi interface),
exactly like `parse_battery` / `parse_disk` in `system.py`, so parsing is
unit-tested against canned strings.

### 3.3 `keep_awake` — background process management (the new abstraction)

`caffeinate` runs until it's killed (or its `-t` timeout elapses), so it does not
fit the one-shot `Runner` (which returns `(rc, out)` for a *completed* command).
`SystemToggles` gets a second injected dependency, a tiny `ProcessManager`:

```python
class ProcessManager(Protocol):
    def start(self, argv: list[str]) -> int: ...   # spawn detached, return pid
    def stop(self, pid: int) -> None: ...           # terminate (SIGTERM)
    def running(self, pid: int) -> bool: ...        # is it still alive?
```

Default impl wraps `subprocess.Popen` + `os.kill`; a fake is injected in tests.
`SystemToggles` tracks the active caffeinate **pid in-memory** (the daemon is a
long-lived singleton, so it persists across turns in a session):

- `keep_awake(minutes=30)` → stop any existing one, `start(["caffeinate","-dimsu","-t","1800"])`,
  remember the pid → *"I'll keep your Mac awake for 30 minutes."* (`-t` self-expires.)
- `keep_awake()` (no minutes) → `start(["caffeinate","-dimsu"])` (indefinite) →
  *"I'll keep your Mac awake until you tell me to stop."*
- `keep_awake(off=True)` → stop the tracked pid → *"Okay, your Mac can sleep
  normally again."* (or a note if nothing was active).
- Starting a new keep-awake always stops the previous one first, so we never leak
  caffeinate processes.

**Restart caveat (accepted):** the pid is in-memory, so a daemon restart loses the
handle. Timed (`-t`) processes self-terminate; an indefinite one could outlive the
daemon. Mitigation kept simple for v1 — caffeinate is spawned as a child of the
daemon (dies with it on a clean exit); cross-restart recovery is noted as possible
future work, not built now.

### 3.4 Graceful degradation & runtime permissions (the fragile three)

Three tools have more than one path with different permission/availability, so
their `requires` is **left unset** and the outcome is handled *inside the handler*
as a friendly string (a static `requires` would wrongly block the no-permission
path):

- **`set_brightness`**: absolute + `brightness` binary → `brightness <level/100>`
  (no permission). Else `action` up/down → key-codes 144/145 (needs Accessibility
  at runtime). Else absolute-without-binary → *"…install the brightness tool:
  `brew install brightness`."* Else Accessibility blocked → *"…enable Accessibility
  for Jack in System Settings."*
- **`set_wifi`**: run `networksetup -setairportpower` **without sudo**. If the
  machine's policy requires admin, the command fails → friendly message (*"macOS
  needs admin rights to toggle Wi-Fi on this Mac"*), never a silent `sudo`.
- **`lock_screen`**: try the `CGSession -suspend` binary; if the path is absent
  (`rc 127`, removed on newer macOS) fall back to the AppleScript Ctrl-Cmd-Q
  keystroke (needs Accessibility); if that's blocked → friendly message.

`set_appearance` keeps `requires=automation` — it has a single path that always
targets System Events. Everything else needs no permission.

### 3.5 Wiring

- **`config.py`**: add `allow_system_toggles: bool = True` (on-device → defaults
  on, matching every other local capability; only off-device `allow_web` is off).
- **`app.py::build()`**: after the `allow_system_info` block —
  ```python
  if settings.allow_system_toggles:
      from autobot.tools.toggles import register_system_toggles
      register_system_toggles(registry)
      log.info("system toggles ENABLED (volume/brightness/appearance/sleep/wifi/keep-awake/lock)")
  ```
- **`permissions.py`**: import `AUTOMATION` for `set_appearance.requires`.
- **Logging**: a `get_logger("toggles")` logger; INFO at each seam ("volume set
  to=%d", "appearance mode=%s", "wifi state=%s", "keep_awake minutes=%s", "lock",
  "sleeping"), per the logging rules.

## 4. Decisions (locked)

- **Scope:** volume, brightness, appearance, sleep, Wi-Fi, keep-awake, lock
  screen. **DND deferred** to a new follow-up issue (opened as part of this work,
  linked from #4).
- **Architecture:** Approach A — new `tools/toggles.py` sibling of `system.py`,
  with injected `Runner` + `ProcessManager`.
- **Risk:** all seven `WRITE` → audited, **no confirmation** (all reversible; a
  voice "turn it up" must not prompt). Gate confirms only `DESTRUCTIVE`.
- **No silent escalation:** `set_wifi` never runs `sudo`; it degrades to a message.
- **Fragile tools** (`set_brightness`, `set_wifi`, `lock_screen`) degrade
  gracefully; `requires` left unset, permission handled at runtime.
- **`keep_awake`** uses an injected `ProcessManager`, in-memory pid tracking,
  always replacing any prior keep-awake.
- **Tool shape:** one tool per domain; `set_volume`/`set_brightness` take optional
  `level` + optional `action`.
- **Config:** `allow_system_toggles: bool = True`.

## 5. Testing

New `tests/unit/test_toggles.py`, fully offline via a `FakeRunner` and a
`FakeProcessManager` (mirrors `test_system.py` / `test_trash.py`):

- **Pure helpers:** `parse_volume_settings`, clamp (130 → 100, -5 → 0), appearance
  read parsing, Wi-Fi power parsing.
- **set_volume:** absolute calls `osascript` with the right level; up/down read
  current then set current±step (clamped at the ends); mute/unmute set
  `output muted`; out-of-range / no-args → friendly strings.
- **set_brightness:** binary present → `brightness level/100`; binary absent
  (`rc 127`) + absolute → setup message; up/down → key-code 144/145; Accessibility
  error → friendly note.
- **set_appearance:** dark/light force `true`/`false`; toggle uses `not dark mode`;
  reports the resulting mode.
- **sleep_mac:** calls `pmset sleepnow`; non-zero rc → friendly failure.
- **set_wifi:** on/off call `-setairportpower <dev> on|off`; toggle reads power
  then flips; admin-required failure → friendly message (and no sudo in argv).
- **keep_awake:** `minutes` → `ProcessManager.start` with `-t N` and pid tracked;
  indefinite → start without `-t`; `off` → `stop(pid)`; a second start stops the
  first (no leak).
- **lock_screen:** CGSession path tried first; `rc 127` → keystroke fallback;
  fallback blocked → friendly message.
- **Registration/specs:** all seven registered, all `Risk.WRITE`,
  `set_appearance.requires == AUTOMATION`, others `None`; no confirmation needed.
- `make check` (ruff, ruff-format, mypy strict, pytest) stays green.

## 6. Research & prior art (cited)

A web review of what's controllable on-device on macOS Sequoia, and how reliable
each path is.

**Volume — solid, no permission.** `set volume output volume N` (0–100),
`set volume output muted true|false`, and reading via `get volume settings` are
Standard Additions commands (not app-targeted), so no Automation prompt.
Refs: [Control OS X volume with AppleScript](https://coolaj86.com/articles/how-to-control-os-x-system-volume-with-applescript/),
[osascript / ss64](https://ss64.com/mac/osascript.html).

**Dark mode — solid, needs Automation.** `tell app "System Events" to tell
appearance preferences to set dark mode to not dark mode` toggles; `set dark mode
to true|false` forces. Idempotent, no daemon kick, works Mojave → current.
Ref: [Toggle macOS Dark Mode from the command line](https://techearl.com/mac-dark-mode-command-line).

**Sleep — solid.** `pmset sleepnow` (a binary, no app target, no permission).
Reversible — waking the Mac restores everything; hence `WRITE`, not destructive.
Ref: [osascript / ss64](https://ss64.com/mac/osascript.html).

**Keep-awake — solid, no permission.** `caffeinate` is the built-in power-assertion
tool; `-d -i -m -s -u` cover display/idle/disk/system sleep + user-active, and
`-t <seconds>` auto-expires. It's a long-running process, so it needs spawn/track/
kill management (§3.3). Refs: `man caffeinate`,
[caffeinate usage notes](https://ss64.com/mac/caffeinate.html).

**Brightness — no native CLI (fragile).** Either the Homebrew
[`brightness`](https://github.com/nriley/brightness) binary (precise `0.0–1.0`,
external install) or AppleScript key-codes 144/145 (one notch, needs Accessibility).
Both work on Sequoia; neither is built in → graceful degradation (§3.4).
Refs: [nriley/brightness](https://github.com/nriley/brightness),
[Adjust screen brightness from the command line](https://osxdaily.com/2019/08/14/change-screen-brightness-mac-terminal/).

**Wi-Fi on/off — works, may need admin.** `networksetup -setairportpower <dev>
on|off` toggles power; `-getairportpower` reads it. Docs commonly show `sudo`, and
admin can be *required* by the `RequireAdminPowerToggle` policy — so the tool runs
it un-elevated and degrades to a message rather than ever escalating. Device is
resolved via `-listallhardwareports` (fallback en0), reusing system.py's parser.
Refs: [Enable/Disable AirPort from the command line](https://osxdaily.com/2011/05/31/enable-disable-airport-wireless-connections-command-line/),
[networksetup for Wi-Fi](https://support.moonpoint.com/os/os-x/networksetup-wifi.php).

**Lock screen — fragile.** The classic `CGSession -suspend`
(`…/Menu Extras/User.menu/Contents/Resources/CGSession`) still works where the
path exists, but that path has been removed on newer macOS; the reliable modern
path is the AppleScript Ctrl-Cmd-Q keystroke (needs Accessibility) → graceful
degradation (§3.4). Ref: [Lock the Mac desktop from the command line](https://osxdaily.com/2012/03/30/lock-mac-desktop-command-line/).

**Do Not Disturb / Focus — no reliable native CLI (deferred).** The old
`defaults write com.apple.ncprefs …` hack broke after Big Sur; on Ventura+/Sequoia
the robust path is **Shortcuts** — create a "Set Focus" shortcut once, then
`shortcuts run "<name>"`. That one-time setup and bridge design is its own issue.
Refs: [Turn on Focus mode from the Terminal](https://heyfocus.com/blog/how-to-turn-on-mac-focus-mode-from-the-terminal/),
[Apple — Turn a Focus on or off on Mac](https://support.apple.com/guide/mac-help/turn-a-focus-on-or-off-mchl999b7c1a/mac).

## 7. Risks

- **Brightness / lock portability** — the `brightness` binary, AppleScript
  key-codes, and `CGSession` path vary across Apple Silicon, external displays,
  and macOS versions. Mitigated by graceful degradation + always returning a
  friendly, actionable message (never a raw failure).
- **Wi-Fi admin policy** — on Macs configured with `RequireAdminPowerToggle`, the
  toggle fails without admin. Accepted: we never `sudo`; the tool says so plainly.
  Reading status (existing tool) is unaffected.
- **AppleScript permission friction** — first `set_appearance` (and the brightness/
  lock fallbacks) can trip Automation/Accessibility prompts. The gate opens the
  right pane for `requires`; the fragile tools explain themselves at runtime.
- **caffeinate orphan on daemon restart** — in-memory pid tracking loses an
  indefinite keep-awake across a restart. Bounded: timed ones self-expire; child
  dies on clean daemon exit; cross-restart recovery deferred (§8).
- **Tool-surface pressure** — +7 tools pushes the registry further past the
  ~20–30 where small-model selection accuracy degrades; tight tool descriptions
  mitigate, consolidation tracked separately.
- **Sleep / lock are disruptive** — they blank/lock the screen immediately.
  Accepted: only on explicit request, fully reversible, and confirming "go to
  sleep" would be worse UX than the action.

## 8. Future work (separate issues)

- **Do Not Disturb / Focus** — new follow-up issue. Shortcuts bridge:
  `shortcuts run`, detect whether the needed shortcut exists (`shortcuts list`),
  and guide a one-time setup when it doesn't. Privacy-clean (Shortcuts runs
  locally).
- **Keep-awake cross-restart recovery** — persist the caffeinate pid (e.g. under
  `~/.autobot/`) so a restarted daemon can find and stop an indefinite keep-awake.
- **More controls if wanted** — Bluetooth (needs `blueutil`/private API), Night
  Shift / True Tone (CoreBrightness private API), keyboard backlight. Each needs
  its own feasibility pass; excluded here for lack of a clean on-device path.
