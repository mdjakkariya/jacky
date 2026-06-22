# Assistent — "Jack"

A privacy-first, **on-device** voice assistant for macOS. You talk to a floating
orb ("Jack"); it listens, understands, and acts on your Mac — open and close apps
and websites, read the time, manage files, check battery/Wi-Fi/disk, remember
things about you, empty the Trash. Everything runs locally: **no audio, text, or
memory leaves your machine**, with two clearly-marked, opt-in exceptions (web
search and the optional cloud LLM).

**English only**, both directions (speech-in and speech-out). Engineering
conventions live in [`CLAUDE.md`](CLAUDE.md); the build history is in
[`docs/plans/`](docs/plans/).

---

## What it can do

- **Hands-free** — say **"jack, …"** and it listens, transcribes, and acts. Or
  push-to-talk.
- **Talks back** — on-device Piper text-to-speech, with a bundled default voice.
- **Barge-in** — talk over Jack to interrupt it (full-duplex when echo
  cancellation is available; otherwise it finishes speaking, then listens).
- **Acts through tools, never blindly** — every action runs through a permission
  gate: read-only tools run straight through, destructive ones ask first, and
  everything is audited.
- **App & web control** — open/focus/hide/minimize/quit/uninstall apps, open a
  website (`open YouTube`), close a browser tab (`close youtube.com`).
- **System & memory** — battery/Wi-Fi/disk; remembers your name and preferences.
- **Cloud option** — runs fully local on Ollama by default, or switch to Claude.

---

## Try it (dev preview)

Grab the latest [GitHub Release](../../releases) and download the **`.dmg`** — it's
a single, self-contained app: the orb plus the embedded engine and a default
voice. Drag **Jack** to Applications.

It's an **unsigned dev preview**, so the first launch is: right-click **Jack** →
**Open** → **Open** (after that it launches normally).

On first run Jack walks you through setup (LLM provider, models, permissions). You
still need, locally:

- **Ollama** running with a model (local mode, default), *or* an **Anthropic API
  key** (cloud mode). Set this in the orb's **Settings**.
- macOS **permissions** — Microphone (to hear you), and **Automation** /
  **Accessibility** for controlling apps. Jack's **Settings → Permissions** tab
  shows each one's status and links straight to the right System Settings pane; if
  a tool needs a permission you haven't granted, Jack says so and opens it for you.

Maintainers: cutting a release is documented in [`docs/RELEASING.md`](docs/RELEASING.md);
packaging internals in [`docs/PACKAGING.md`](docs/PACKAGING.md).

---

## Architecture

A pipeline of swappable stages, each a `Protocol` in
`src/autobot/core/interfaces.py`, driven by an orchestrator state machine. The
language model **plans** tool calls; the **permission gate** executes them — the
model never runs a tool directly.

```
Orchestrator (idle → listening → transcribing → planning → executing → responding)
  AudioSource ─▶ SpeechToText ─▶ LanguageModel ─plan─▶ PermissionGate ─▶ ToolRegistry
   wake+VAD       faster-whisper   Ollama (local)       risk? permission?   apps, web,
   or PTT         or whisper.cpp   or Claude (cloud)     confirm? audit      files, system…
   (+AEC mic)                                                                      │
                                          TextToSpeech (Piper) ◀── reply ◀─────────┘
                                          (barge-in: talk to interrupt)
```

The engine is **headless**; the macOS **orb** (a Tauri app) is a thin client that
talks to it over a localhost WebSocket and owns its lifecycle (spawns it, shows
state, hosts Settings). Concrete implementations live in `io/`, `stt/`, `llm/`,
`tts/`, `tools/`, `orchestrator/`, `daemon/`, wired together in one place —
`src/autobot/app.py::build()`. Swapping a model, backend, or policy is a one-line
change there.

```
src/autobot/
  core/         interfaces (Protocols) + value types + Risk/State/Decision enums
  config.py     typed Settings; the single source of defaults (settings.json overlay)
  permissions.py central macOS permission tracking (mic / accessibility / automation)
  io/           audio capture: wake-word + VAD, push-to-talk, AEC mic (endpointing is pure)
  stt/          speech-to-text: faster-whisper (CPU) or whisper.cpp (Metal); English-only
  llm/          Ollama tool-calling client + optional Anthropic cloud client
  tts/          Piper voice output (interruptible) + pluggable audio player
  tools/        registry, permission gate, sandbox, audit log, app/web/system/memory tools
  orchestrator/ state machine + turn loop (the backbone)
  daemon/       localhost WebSocket server + settings/permissions/report API
  diagnostics.py breadcrumb ring buffer + shareable debug report
  app.py        composition root + run loop
ui/orb/         the orb web client + Settings view
ui/orb-shell/   the Tauri (Rust) shell that hosts the orb and the engine sidecar
tests/unit/     fast tests, no model runtime or mic required
```

---

## Build from source (macOS, Apple Silicon)

Tested target: MacBook Air M2, 16 GB, macOS 15.

1. **Install [uv](https://docs.astral.sh/uv/) and Ollama:**

   ```bash
   brew install uv ollama
   ```

   > No PortAudio install needed — the `sounddevice` wheel bundles it.

2. **Start Ollama and pull the model** (`qwen3:8b` is the default — a reliable
   small tool-caller):

   ```bash
   ollama serve            # leave running in its own tab
   ollama pull qwen3:8b    # ~5 GB, one time
   ```

3. **Create the dev environment** (dev tools + hands-free, voice, daemon, cloud,
   whisper.cpp, and AEC extras + git hooks):

   ```bash
   cd /path/to/autobot
   make setup
   ```

   > `uv sync` **replaces** the installed extra set, so never sync a single extra
   > on its own (it drops the others). `make setup`/`make install` sync the whole
   > set. Push-to-talk needs no extras (`"input_mode": "ptt"`).

4. **Download a Piper voice** (voice output is on by default; the default is the
   male "Ryan" voice). The bundled `.dmg` ships this automatically — for a source
   run, fetch it once:

   ```bash
   mkdir -p ~/.autobot/voices && cd ~/.autobot/voices
   uv run python -m piper.download_voices en_US-ryan-high
   ```

   Pick any with `"tts_voice": "~/.autobot/voices/<name>.onnx"`, or turn voice off
   with `"tts_enabled": false`. Browse the
   [Piper samples](https://rhasspy.github.io/piper-samples/).

5. **Run the engine, then the orb:**

   ```bash
   make run            # the engine/daemon (uv run autobot-daemon)
   ```

   In dev the engine runs on its own; build/run the orb separately from
   `ui/orb-shell` (`cargo tauri dev`). The first run prompts for **Microphone**
   permission and downloads the STT weights.

---

## Talking to Jack

**Hands-free (default):** start with **"jack"** — *"jack, what's the time"* — said
naturally, fast or with a pause. Jack transcribes each phrase and, if the wake word
appears, strips it and runs the rest. "jack" is used because the STT model
transcribes it reliably; matching is on the text, not an acoustic threshold, so
continuous speech works. VAD ends the clip when you stop.

**Follow-ups:** after a reply Jack keeps listening **without** the wake word for a
window (default **30s**, measured from when it finishes speaking; each turn resets
it). Speak again and it just answers; stay quiet and it lapses back to needing the
wake word. Tune with `follow_up_window_s` (`0` to always require the wake word).

**Dismiss:** say *"go away"* / *"you're done"* and Jack hides; only the wake word
brings it back. It also auto-hides when idle.

**Barge-in:** talk over Jack to interrupt. This is reliable only when its own voice
is cancelled from the mic — automatic with **AEC** (`aec` on, default) when macOS
Voice-Processing initializes, or with headphones. Otherwise Jack runs half-duplex
(finishes speaking, then listens), which never mishears itself.

Things to try:

- *"what time is it"* — read-only, runs straight through.
- *"open YouTube"* / *"close youtube.com"* — opens a site / closes that tab.
- *"open Spotify"*, *"quit Mail"*, *"minimize Safari"* — app control.
- *"how's my battery"*, *"what's my Wi-Fi"* — system info.
- *"create a file called notes.txt that says hello"* — a WRITE action; audited.
- *"empty the trash"* — DESTRUCTIVE; Jack confirms first (answer by voice or the
  card under the orb), then does it.

File tools are confined to the workspace dir (`~/.autobot/workspace`) and every
attempt is recorded in the audit log (`~/.autobot/audit.db`).

---

## Configuration

All tunables live in one JSON file, **`~/.autobot/settings.json`** — there are
**no environment variables**. A missing file or key falls back to the built-in
default; setting names are the field names in `src/autobot/config.py` (the single
source). The orb's **Settings** view edits the common ones live (no restart).

```json
{ "llm_provider": "ollama", "llm_model": "qwen3:8b",
  "stt_engine": "faster_whisper", "stt_model": "small.en",
  "follow_up_window_s": 30, "barge_in": true, "aec": true }
```

Secrets (API keys) are **never** in this file — they live in the macOS **Keychain**
(service `autobot`), set via the Settings view or `security`.

**Local vs. cloud LLM:** `"llm_provider": "ollama"` (default, fully on-device) or
`"anthropic"` (Claude — sends the conversation + tool schemas to Anthropic, never
audio, never the actions; opt-in and disclosed). Set the model with `llm_model` /
`anthropic_model` and the key in the Keychain.

**Speech engine:** `faster_whisper` (CPU/int8, default) or `whisper_cpp` (Metal/GPU
on Apple Silicon — runs bigger models like `medium.en` far faster; needs the
`whispercpp` extra). Both are hot-swappable from Settings. Bias recognition toward
your apps with `stt_prompt`.

---

## Permissions

Jack needs a few macOS permissions and manages them in one place — **Settings →
Permissions** shows each with its status and a button that opens the exact System
Settings pane:

- **Microphone** — to hear you.
- **Accessibility** — show/hide/minimize/list app windows.
- **Automation** — control other apps via Apple Events (close browser tabs, quit
  apps, empty the Trash).

If a tool needs a permission that isn't granted, Jack refuses cleanly ("I don't
have Automation permission yet…"), opens the right pane, and you grant it once.

> In a **source/dev run** the engine's permissions are attributed to whatever
> launched it (your terminal or VS Code), not "Jack" — so you'll grant those apps.
> The shipped `.dmg` gets its own "Jack" entry.

---

## Web search (optional — an off-device feature)

Off by default. When enabled, it sends only your search *query* to a provider; the
local LLM summarizes the results.

```bash
make install   # extras include web; or: uv sync ... --extra web
# set "allow_web": true in settings.json, then store a key (optional):
security add-generic-password -U -s autobot -a web_api_key -w 'YOUR-KEY'
```

`web_provider`: `auto` (use the keyed API if a key is set, else fall back to free
`ddgs` scraping), `searchspace` (force API), or `ddgs` (force scraping).

---

## Development

```bash
make check    # ruff lint + format-check + mypy (strict) + pytest — run before committing
make test     # pytest with coverage
make format   # auto-format and auto-fix
make help     # list all targets
```

Quality gates: **ruff** (lint + format), **mypy strict**, **pytest** — enforced by
**pre-commit** and **GitHub Actions** (`.github/workflows/ci.yml`). See `CLAUDE.md`
for the conventions.

---

## Logs & debugging

**Debug report (easiest):** in the orb, **Settings → Raise an issue** (or the tray
menu) builds a compact, **redacted** snapshot — recent events, errors, the state
sequence, config (no audio, no API keys) — copies it to your clipboard, and can
open a prefilled GitHub issue. Paste and submit.

**Session transcripts** — each run writes a readable Markdown record to
`~/.autobot/sessions/` (auto-pruned to the most recent few).

**Rotating debug log** — the full, terse, all-components trail:

```bash
make logs                   # live tail
make logs-grep C=gate       # one component (gate / listening / stt / llm / …)
tail -n 200 -f ~/.autobot/logs/autobot.log
```

Lines are component-tagged with `key=value` properties:

```
2026-06-23 01:02 INFO [orchestrator] heard text='open youtube' confidence=0.90
2026-06-23 01:02 INFO [gate] allowed tool=open_website risk=WRITE ok=True args={'url': 'youtube.com'}
```

The file always captures DEBUG; to also surface it on the console set
`"log_console_level": "DEBUG"`.

---

## Troubleshooting

- **Engine won't connect / model not found** → ensure `ollama serve` is running and
  you've pulled the `llm_model`, or switch to cloud mode and set the Anthropic key
  in Settings.
- **Jack can't control an app / close a tab** → grant **Automation** (and
  **Accessibility**) in Settings → Permissions; in a dev run grant it for your
  terminal/VS Code.
- **Barge-in interrupts itself / mishears its own voice** → that's AEC not
  cancelling; it falls back to half-duplex, or use headphones. Toggle `barge_in`.
- **No audio / `PortAudioError`** → confirm Microphone permission. If it persists,
  `brew install portaudio` then `uv sync --reinstall-package sounddevice`.
- **Hands-free mode needs the 'wake' extra** → `make install`, or set
  `"input_mode": "ptt"`.
- **A daemon keeps running after quit** → the orb stops its engine on exit; if one
  lingers, `pkill -if autobot-daemon`.
