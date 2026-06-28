# System Controls — Manual Test Guide (issue #4)

How to manually verify the seven write-side macOS control tools by **chat or voice**.
Each tool: prompts to say, the exact reply to expect, how to independently confirm
the Mac actually changed, and the permission prompts you'll hit the first time.

> Tools under test: `set_volume`, `set_brightness`, `set_appearance`, `sleep_mac`,
> `set_wifi`, `keep_awake`, `lock_screen`. All are **WRITE** (audited, no
> confirmation), run **on-device**, and **never use sudo**.

---

## 0. Setup (once)

- [ ] **Ollama running** (the local LLM): `ollama serve` (or the app) — `make run` needs it.
- [ ] **System toggles enabled** — on by default (`allow_system_toggles: true`). If you
      changed it, set it back in `~/.autobot/settings.json` or the Settings view.
- [ ] **(Optional) exact brightness** — install the helper so "set brightness to 40"
      works as a precise level: `brew install brightness`. Without it, you'll get a
      friendly "install brightness" hint and only relative "brighter/dimmer" works.
- [ ] **Launch Jack:** `make run`. Chat is the default — type the prompts below into the
      chat drawer. (Voice works the same way: the model maps your spoken words to the
      same tools; say the prompt instead of typing it.)

### Watch what's happening (two terminals help)

- **Component log (per-action seam events):**
  ```bash
  make logs-grep C=toggles
  # or: tail -f ~/.autobot/logs/autobot.log | grep '\[toggles\]'
  ```
  You should see one INFO line per action, e.g. `volume set to=30`, `appearance dark=True`,
  `wifi state=off`, `keep_awake minutes=2 pid=…`, `lock via=…`, `sleeping`.
- **Audit log:** every call is recorded as an allowed WRITE by the permission gate
  (SQLite audit log). Each successful action should appear there too.

### Permissions you'll be asked for (first time only)

| Tool | macOS permission | When |
|---|---|---|
| `set_appearance` | **Automation** (controls System Events) | First dark/light/toggle. The gate will open **System Settings → Privacy & Security → Automation** if it's missing — grant it to Jack, then retry. |
| `set_brightness` (brighter/dimmer) | **Accessibility** | Only the relative key-press path. If missing you get a friendly "enable Accessibility" message. |
| `install_brightness_tool` | none (asks to **confirm** first) | Only if you opt to install the exact-brightness tool — the gate confirms before downloading via Homebrew. |

Volume, sleep, Wi-Fi, keep-awake, and **lock screen** need **no** special permission.

---

## 1. `set_volume` — volume up/down/exact/mute

**Prompts (try several phrasings):**
- [ ] "set the volume to 30"
- [ ] "turn it up" / "make it louder"
- [ ] "turn it down" / "quieter"
- [ ] "set volume to 100"
- [ ] "mute" / "mute the volume"
- [ ] "unmute"

**Expected replies:**
- Exact: `Volume set to 30%.` / `Volume set to 100%.`
- Up/down: `Volume set to N%.` (current ±10, clamped 0–100)
- `Muted.` / `Unmuted.`

**Verify independently:**
```bash
osascript -e 'output volume of (get volume settings)'   # prints the new 0–100 level
osascript -e 'output muted of (get volume settings)'    # true after mute, false after unmute
```
Or watch the volume slider in the menu bar / Control Center.

**Edge cases:**
- [ ] "set volume to 130" → still `Volume set to 100%.` (clamped).
- [ ] "turn it down" repeatedly until it hits `Volume set to 0%.` (clamps at the bottom).

---

## 2. `set_brightness` — exact level or brighter/dimmer

**Prompts:**
- [ ] "make the screen brighter" / "brightness up"
- [ ] "dim the screen" / "make it dimmer"
- [ ] "set brightness to 40"  *(exact — needs the `brightness` binary)*

**Expected replies:**
- Relative: `Brightness turned up.` / `Brightness turned down.` (screen visibly changes)
- Exact **with** binary installed: `Brightness set to 40%.`
- Exact **without** binary: Jack *offers to install it* — `Setting an exact level needs the
  'brightness' tool, which isn't installed. Want me to install it for you? …`
- If Accessibility isn't granted for the relative path: a friendly message pointing you to
  **System Settings → Privacy & Security → Accessibility**.

**Auto-install flow (the new bit):**
- [ ] With the binary **not** installed, say "set brightness to 40" → Jack offers to install.
- [ ] Say "yes, install it" → the model calls `install_brightness_tool`, and the **permission
      gate asks you to confirm** ("Install the 'brightness' tool via Homebrew? This downloads
      it from the internet…"). Confirm.
- [ ] Jack runs `brew install brightness`, then you can say "set brightness to 40" again →
      `Brightness set to 40%.` (Afterwards exact levels just work.)
- [ ] If Homebrew isn't installed at all, Jack points you to **https://brew.sh** instead of
      failing — it never tries to bootstrap Homebrew itself, and never opens the URL for you.

**Verify independently:**
- Watch the screen brightness change, or the Control Center brightness slider.
- After install: `brightness -l` prints the current level (0.0–1.0); `which brightness` resolves.

**Edge cases:**
- [ ] Decline the install confirmation → nothing is installed; Jack just acknowledges.
- [ ] First "make it brighter" may prompt for Accessibility — grant it, then retry.

---

## 3. `set_appearance` — dark / light / toggle

**Prompts:**
- [ ] "switch to dark mode" / "go dark"
- [ ] "switch to light mode" / "go light"
- [ ] "toggle dark mode" / "flip the appearance"

**Expected replies:**
- `Now in dark mode.` / `Now in light mode.` (the whole UI switches instantly, no restart)
- Toggle flips to whichever is opposite of the current state.

**Verify independently:**
```bash
defaults read -g AppleInterfaceStyle    # prints "Dark" in dark mode; errors ("does not exist") in light mode
```
Or just look — the menu bar, Dock, and windows switch theme immediately.

**First-run permission:** the first appearance change needs **Automation**. If it's not
granted, the gate opens **System Settings → Privacy & Security → Automation**; enable Jack's
access to **System Events**, then say the prompt again.

---

## 4. `sleep_mac` — sleep now

> ⚠️ This blanks the screen and sleeps the Mac immediately. Do this test **last**, and
> make sure nothing is mid-save.

**Prompts:**
- [ ] "go to sleep" / "put the Mac to sleep" / "sleep now"

**Expected reply:** `Going to sleep.` (then the Mac sleeps)

**Verify independently:** the display turns off and the Mac sleeps within ~1s. Press a key /
the Touch ID button to wake it — everything is exactly as you left it (fully reversible).

---

## 5. `set_wifi` — on / off / toggle

**Prompts:**
- [ ] "turn off Wi-Fi"
- [ ] "turn Wi-Fi back on" / "enable Wi-Fi"
- [ ] "toggle Wi-Fi"

**Expected replies:**
- `Wi-Fi turned off.` / `Wi-Fi turned on.`
- On a locked-down Mac that requires admin to toggle Wi-Fi: `macOS needs admin rights to
  toggle Wi-Fi on this Mac, so I can't do it automatically.` (This is expected — Jack
  **never** uses sudo.)

**Verify independently:**
```bash
networksetup -getairportpower en0      # "Wi-Fi Power (en0): Off"  then  "...: On"
```
Or watch the Wi-Fi icon in the menu bar. (Note: if Wi-Fi was your only connection,
turning it off drops the network — turn it back on to continue.)

**Edge cases:**
- [ ] "toggle Wi-Fi" when it's **on** → turns off; when it's **off** → turns on.

---

## 6. `keep_awake` — caffeinate (timed or indefinite) + stop

**Prompts:**
- [ ] "keep my Mac awake for 2 minutes"   *(timed)*
- [ ] "keep my Mac awake" / "don't let it sleep"   *(indefinite)*
- [ ] "stop keeping my Mac awake" / "let it sleep normally"   *(off)*

**Expected replies:**
- Timed: `I'll keep your Mac awake for 2 minutes.`
- Indefinite: `I'll keep your Mac awake until you tell me to stop.`
- Off (while active): `Okay, your Mac can sleep normally again.`
- Off (nothing active): `Your Mac wasn't being kept awake.`

**Verify independently:**
```bash
pgrep -l caffeinate                                   # a caffeinate process appears while active
pmset -g assertions | grep -i -E 'caffeinate|PreventUserIdleSystemSleep'   # assertion held while active
```
After "stop keeping awake", `pgrep caffeinate` should return nothing. A **timed** session
ends on its own after the window (`pgrep caffeinate` empties automatically).

**Edge cases:**
- [ ] Start indefinite, then start a 2-minute one → the first is replaced (only **one**
      `caffeinate` process at a time; check `pgrep -l caffeinate` shows a single PID).
- [ ] "stop keeping awake" when none is active → `Your Mac wasn't being kept awake.`

> Note: keep-awake tracking is in-memory. If you restart the Jack daemon while an
> **indefinite** keep-awake is running, Jack forgets it (a timed one still self-expires).
> Documented limitation; cross-restart recovery is future work.

---

## 7. `lock_screen` — lock now

**Prompts:**
- [ ] "lock my screen" / "lock the Mac"

**Expected reply:** `Locking the screen.` (the display turns off / lock screen appears)

**Verify independently:** the display sleeps and the Mac locks; log back in with your
password / Touch ID. **The Jack window must stay open** (the earlier bug where it quit the
app is fixed).

**How it works now:** on macOS where the legacy `CGSession` lock path exists, that's used;
otherwise Jack runs `pmset displaysleepnow` — it sleeps the display, which **locks if you
have "Require password after sleep" set** (System Settings → Lock Screen → "Require password
after screen saver begins or display is turned off" = *Immediately*). No Accessibility
needed. (We deliberately removed the old Ctrl-Cmd-Q keystroke fallback — it was being
delivered to the frontmost app as Cmd-Q and quitting it.)

**Edge case:**
- [ ] If you've set "Require password" to *Never*, the display still sleeps but won't lock —
      that's a macOS security setting, not Jack. Set it to *Immediately* for a true lock.

---

## Quick regression checklist

| # | Tool | Prompt | Expect | Confirm with |
|---|------|--------|--------|--------------|
| 1 | volume | "set volume to 30" | `Volume set to 30%.` | `osascript -e 'output volume of (get volume settings)'` |
| 1 | volume | "mute" / "unmute" | `Muted.` / `Unmuted.` | `osascript -e 'output muted of (get volume settings)'` |
| 2 | brightness | "make it dimmer" | `Brightness turned down.` | screen dims |
| 2 | brightness | "set brightness to 40" | exact % or **offer to install** | `brightness -l` (after install) |
| 2 | brightness | "yes install it" → confirm | gate confirms → installs → set exact | `which brightness` resolves |
| 3 | appearance | "go dark" / "go light" | `Now in dark/light mode.` | `defaults read -g AppleInterfaceStyle` |
| 4 | sleep | "go to sleep" | `Going to sleep.` | Mac sleeps (wake to confirm) |
| 5 | wifi | "turn off Wi-Fi" | `Wi-Fi turned off.` | `networksetup -getairportpower en0` |
| 6 | keep_awake | "keep my Mac awake for 2 minutes" | `…for 2 minutes.` | `pgrep -l caffeinate` |
| 6 | keep_awake | "stop keeping awake" | `…sleep normally again.` | `pgrep caffeinate` empty |
| 7 | lock | "lock my screen" | `Locking the screen.` (UI stays open!) | screen locks (needs "require password") |

**Privacy spot-check (optional):** while testing, confirm nothing phones home —
`make logs-grep C=toggles` should show only local command seams, and every command is a
local binary (`osascript` / `pmset` / `networksetup` / `caffeinate` / `brightness` /
`CGSession`). No network, no sudo.

---

## If something doesn't work

- **"I need Automation/Accessibility access…"** → grant it in **System Settings → Privacy &
  Security** for Jack, then repeat the prompt.
- **"macOS needs admin rights to toggle Wi-Fi…"** → expected on Macs with the admin-toggle
  policy; nothing to fix (we won't sudo).
- **Exact brightness not working** → say "yes, install it" when Jack offers; confirm the gate
  prompt and it'll `brew install brightness`. No Homebrew? Jack links you to https://brew.sh.
- **"lock my screen" only blanks the display** → set System Settings → Lock Screen → require
  password = *Immediately*. Jack uses display-sleep (it no longer sends a keystroke that
  could quit the front app).
- **Nothing happens / wrong tool chosen** → check `make logs-grep C=toggles` and `[llm]` logs
  to see which tool the model picked; rephrase more directly ("set the volume to 20").
