# Autobot

A privacy-first, zero-cost, **on-device** voice assistant. Everything runs locally — no audio, text, or memory leaves your machine. Build sequence and rationale live in [`docs/plans/autobot_build_roadmap.md`](docs/plans/autobot_build_roadmap.md); engineering conventions live in [`CLAUDE.md`](CLAUDE.md).

**Constraint:** English only, both directions (STT and TTS).

**Status:** Phase 2 complete — hands-free wake word + VAD listening (swappable with push-to-talk), on top of the Phase 1 orchestrator + permission gate.

---

## Architecture

A pipeline of swappable stages, each defined as a `Protocol` in `src/autobot/core/interfaces.py`, driven by an orchestrator state machine. The language model **plans** tool calls; the **permission gate** executes them:

```
Orchestrator (state machine: idle→listening→transcribing→planning→executing→responding)
  AudioSource ─▶ SpeechToText ─▶ LanguageModel ─plan─▶ PermissionGate ─▶ ToolRegistry
   wake word      (base.en)       (Ollama)             (risk? confirm?      (get_time,
   + VAD / PTT                                          audit, sandbox)     create/move/delete)
```

The model never runs tools directly — it calls an executor the orchestrator wires to the gate. Input is hands-free (wake word + VAD) or push-to-talk; both satisfy the same `AudioSource` contract. Concrete implementations live in `io/`, `stt/`, `llm/`, `tools/`, `orchestrator/` and are wired together in one place — `src/autobot/app.py::build()`. Swapping a model, back-end, or policy is a one-line change there.

```
src/autobot/
  core/         interfaces (Protocols) + value types + Risk/State/Decision enums
  config.py     typed Settings; the only place env vars are read
  io/           audio capture: push-to-talk + wake-word/VAD (endpointing is pure & tested)
  stt/          speech-to-text (English-only)
  llm/          Ollama tool-calling client + pure parsing helpers
  tools/        registry, permission gate, sandbox, audit log, built-in + fs tools
  orchestrator/ state machine + turn loop (the backbone)
  app.py        composition root + run loop
tests/unit/     fast tests, no model runtime or mic required
```

---

## Setup (macOS, Apple Silicon)

Tested target: MacBook Air M2, 16 GB, macOS 15.

1. **Install [uv](https://docs.astral.sh/uv/)** (Python project manager) and **Ollama**:

   ```bash
   brew install uv ollama
   ```

   > No PortAudio install needed — the `sounddevice` wheel bundles it. (Only if a source build is ever forced do you need `brew install portaudio`.)

2. **Start Ollama and pull the model** (`qwen3:8b` is the default — the most reliable small tool-caller; thinking-mode is disabled for speed):

   ```bash
   ollama serve            # leave running in its own terminal tab
   ollama pull qwen3:8b    # ~5 GB, one time
   ```

3. **Create the dev environment** — installs dev tools **and** the hands-free + voice extras (silero-VAD, openWakeWord, Piper) plus git hooks:

   ```bash
   cd /path/to/autobot
   make setup        # = uv sync --extra dev --extra all
   ```

   > Don't run `uv sync --extra tts` (or `--extra wake`) on their own — `uv sync` replaces the installed set, so a single extra *drops* the others. Use `--extra all` (or `make setup`) to get everything. Push-to-talk needs no extras (`AUTOBOT_INPUT=ptt`).

4. **Download a Piper voice** for voice output (on by default). The default is a **male** voice (Ryan):

   ```bash
   mkdir -p ~/.autobot/voices && cd ~/.autobot/voices
   uv run python -m piper.download_voices en_US-ryan-high       # male (default)
   # other options:
   #   en_US-ryan-medium     (male, lighter/faster)
   #   en_GB-alan-medium     (British male)
   #   en_US-lessac-medium   (female)
   ```

   Pick any with `AUTOBOT_TTS_VOICE=~/.autobot/voices/<name>.onnx`, or turn voice off with `AUTOBOT_TTS=0`. Browse voices at the [Piper samples page](https://rhasspy.github.io/piper-samples/). If the voice/extra is missing, Autobot runs text-only and prints `[tts] voice output OFF …` at startup.

   *(Only if you switch to the `openwakeword` detector: `uv run python -c "import openwakeword.utils as u; u.download_models()"`. The default `stt` detector doesn't need it.)*

6. **Microphone permission:** the first run prompts macOS to let your terminal use the mic — allow it (System Settings → Privacy & Security → Microphone). The first run also downloads the `base.en` whisper weights (~150 MB).

---

## Run

```bash
make run            # or: uv run autobot   /   uv run python -m autobot
```

**Hands-free (default):** start with **"jack"** — e.g. *"jack, what's the time"* — said naturally, fast or with a pause; it all works. Autobot transcribes each phrase and, if it starts with the wake word, strips it and runs the rest as your command (the `stt` detector). "jack" is used because it's a common word the STT model transcribes reliably; continuous and fast speech work because matching is on the text, not an acoustic threshold. VAD cuts the clip when you stop. You'll see live `[state]` transitions, the transcription, and the reply.

**Conversational follow-ups:** after a reply, Autobot keeps listening for a follow-up **without** the wake word for a window (default **20s**, measured from when it finishes speaking; each turn resets it). Speak again and it just answers; stay quiet and it lapses back to needing the wake word — a natural back-and-forth. Tune with `AUTOBOT_FOLLOWUP_WINDOW_S` (`0` to always require the wake word; lower it if it picks up nearby chatter).

**Spoken acknowledgements:** before running a tool (especially a slow one like web search), Autobot says a quick "On it…" / "Let me look that up." so you're not left in silence. Disable with `AUTOBOT_ACK=0`.

**Push-to-talk:** `AUTOBOT_INPUT=ptt make run` — press Enter to start/stop recording instead.

Either way, try:

- *"what time is it"* — read-only tool, runs straight through.
- *"create a file called notes.txt that says hello"* — a WRITE action; runs and is audited.
- *"delete notes.txt"* — a DESTRUCTIVE action; prompts `⚠ … Proceed? [y/N]` and only runs on `y`.

Acting tools are confined to the workspace dir (`~/.autobot/workspace` by default) and every attempt is recorded in the audit log (`~/.autobot/audit.db`). Override with `AUTOBOT_SANDBOX_DIR` / `AUTOBOT_AUDIT_DB`.

### Wake word

There are two wake detectors, selected with `AUTOBOT_WAKE_DETECTOR`:

- **`stt`** (default) — transcribe-then-match: the wake word is matched on the transcript (`AUTOBOT_WAKE_PHRASE`, default `jack`). Handles continuous and fast speech naturally (matching is on text, not an acoustic threshold). **Choose a common word the STT model transcribes reliably** — "jack" works well; rare proper nouns like "jarvis" get mis-transcribed by `base.en`. The last word of the phrase is the trigger, so "hey jack" also matches. Tradeoff: every nearby phrase is transcribed to check it (more CPU, still fully on-device).
- **`openwakeword`** — a dedicated acoustic wake-word model, independent of transcription. Needs a pretrained or custom-trained model for your phrase (`AUTOBOT_WAKE_MODEL`: `hey_jarvis`, `alexa`, `hey_mycroft`, …; custom phrases need offline [training](https://github.com/dscripka/openWakeWord)). Tune with `AUTOBOT_WAKE_THRESHOLD` (lower = more sensitive; a continuously-spoken wake word peaks lower than an isolated one) and `AUTOBOT_WAKE_PREROLL_MS`.

Common tuning: `AUTOBOT_VAD_THRESHOLD`, `AUTOBOT_END_SILENCE_MS` (raise if you're cut off mid-sentence), `AUTOBOT_FOLLOWUP_WINDOW_S`.

### Configuration

All tunables live in one JSON file, **`~/.autobot/settings.json`** — there are
**no environment variables**. Missing file or key → the built-in default. The
setting names are the field names in `src/autobot/config.py` (the single source
of defaults). For example:

```json
{ "llm_model": "qwen2.5:3b", "stt_model": "base.en", "stt_beam_size": 1,
  "save_audio": true, "follow_up_window_s": 20 }
```

Secrets (API keys) are **not** in this file — they're stored in the macOS
**Keychain** (`security ... -s autobot -a <name>`). A **Settings view** in the orb
to edit all of this is on the way (see `docs/plans/autobot_cloud_llm_plan.md`).

### Speed vs. accuracy

Default is `qwen3:8b` (most reliable tool-calling). To trade toward speed, set in
`settings.json`: `"llm_model": "qwen2.5:3b"` (or `:1.5b`, fastest/least reliable),
`"stt_model": "base.en"` (faster) or `"medium.en"` (most accurate),
`"stt_beam_size": 1` (greedy). STT defaults to `small.en` beam 5. Set
`"save_audio": true` to dump each captured clip as a WAV in `sessions/`. Reply
length is capped by `llm_max_tokens`. `ollama pull <model>` first.

### Conversation memory

The assistant remembers recent turns so follow-ups have context ("can you search?" → "you sure?" → "Mumbai weather" → it searches Mumbai). It manages the context window dynamically: it detects the model's real window (via Ollama), uses it fully (`num_ctx`), and **summarizes older turns** into a running summary once usage crosses ~85%, keeping the most recent turns verbatim. The check runs **before each turn on an estimate of the upcoming prompt** (and again on the measured tokens after), so a sudden large message can't push a single turn past the limit. Tune with `AUTOBOT_CONTEXT_TOKENS` (0 = auto-detect), `AUTOBOT_COMPACT_AT` (default 0.85), `AUTOBOT_KEEP_RECENT` (default 6). Per-turn usage shows as `[ctx] N/M tokens (P%)`. Memory is in-session (resets on restart).

### Web search (optional — the only off-device feature)

Everything above is on-device. Web search is the **one exception** and is **off by default**: it sends your search *query* to a search provider, then the local LLM summarizes the results. Enable it explicitly:

```bash
# install ddgs (the fallback) while keeping the other extras:
uv sync --extra dev --extra all --extra web
# set "allow_web": true in ~/.autobot/settings.json, then store your key once:
security add-generic-password -U -s autobot -a web_api_key -w 'YOUR-KEY'
uv run autobot
# "jack what's the weather in Bengaluru"
```

Enable it by setting `allow_web: true` in `~/.autobot/settings.json`. The **API key is never in settings or source** — it's stored in the macOS **Keychain** (service `autobot`, account `web_api_key`) via `autobot.secrets`, and read from there at runtime.

**Backends, configurable with automatic fallback** (`web_provider` in settings.json):

- `auto` (default) — use the keyed HTTP API when a `web_api_key` is in the Keychain (clean, current results); otherwise, or if an API call fails/returns nothing, **fall back to ddgs scraping** (no key).
- `searchspace` forces the API; `ddgs` forces scraping.

Point at any SearchSpace-compatible endpoint via `web_api_url` (default `https://q.searchspace.io/v1/search`). Startup prints `[web] web search ENABLED via API` (or `via ddgs scraping`); each search logs `web via=api/ddgs`. Leave `allow_web` off to stay fully on-device.

---

## Development

```bash
make check    # lint + format-check + mypy (strict) + tests — run before committing
make test     # pytest with coverage
make format   # auto-format and auto-fix
make help     # list all targets
```

Quality gates: **ruff** (lint + format), **mypy strict** (full type checking), **pytest** (with coverage), enforced locally by **pre-commit** and in **GitHub Actions** (`.github/workflows/ci.yml`, active once pushed to GitHub). See `CLAUDE.md` for the conventions these enforce.

---

## Logs & debugging

**Session transcript** — each run writes a readable Markdown record of the whole session (what you said, what Autobot replied, tools used, token usage, compaction, errors) to the **project folder** so it's easy to open and share:

```
sessions/session-YYYYMMDD-HHMMSS.md
```

The terminal also shows a `[ctx] NNN/CTX tokens (P%)` line per turn and a note when compaction triggers (disable with `AUTOBOT_DEBUG=0`; disable the file with `AUTOBOT_SESSION_LOG=0`). Run from the repo so `sessions/` lands there.

**Rotating debug log** — the full, terse, all-components log you can share when something misbehaves:

```
~/.autobot/logs/autobot.log
```

Normal runs keep the terminal clean (only warnings/errors show there); the **full** detail goes to that file. Every line is tagged with the component it came from and logs properties as `key=value`, so it reads clearly:

```
2026-06-19 17:20 INFO    [orchestrator] heard text='what is the time' confidence=0.59
2026-06-19 17:20 INFO    [gate] allowed tool=delete_file risk=DESTRUCTIVE ok=True
2026-06-19 17:20 INFO    [listening] captured seconds=2.2 frames=69
```

Components: `app`, `orchestrator`, `gate`, `stt`, `llm`, `listening`, `wake`.

### View / filter (copy-paste)

```bash
make logs                      # live tail of the whole log
make logs-grep C=gate          # only the permission gate
make logs-grep C=listening     # only wake-word / VAD capture
make logs-grep C=stt           # only speech-to-text
```

Equivalent raw commands if you prefer:

```bash
tail -n 200 -f ~/.autobot/logs/autobot.log     # live tail
grep '\[gate\]'  ~/.autobot/logs/autobot.log   # filter one component
grep -E '\[(stt|llm)\]' ~/.autobot/logs/autobot.log   # a few components
grep -iE 'error|warning' ~/.autobot/logs/autobot.log  # just problems
```

### Sharing with Claude for debugging

When you hit an issue, reproduce it, then send the log (or a filtered slice):

```bash
# whole log
cat ~/.autobot/logs/autobot.log

# or just the relevant feature, e.g. the listening loop
grep '\[listening\]' ~/.autobot/logs/autobot.log
```

Need more detail? The file already captures DEBUG. To also surface debug lines on the console, raise the console level:

```bash
AUTOBOT_LOG_CONSOLE_LEVEL=DEBUG make run
```

Paths and levels are configurable via `AUTOBOT_LOG_DIR`, `AUTOBOT_LOG_LEVEL`, `AUTOBOT_LOG_CONSOLE_LEVEL`.

---

## Troubleshooting

- **`ConnectionError` / model not found** → ensure `ollama serve` is running and you've `ollama pull`ed the model named in `AUTOBOT_LLM_MODEL`.
- **No audio / `PortAudioError`** → confirm the terminal has mic permission. If it persists (rare), `brew install portaudio` then `uv sync --reinstall-package sounddevice`.
- **`Hands-free mode needs the 'wake' extra`** → run `uv sync --extra wake`, or use `AUTOBOT_INPUT=ptt` for push-to-talk.
- **Wake word never triggers** → lower `AUTOBOT_WAKE_THRESHOLD` (e.g. `0.3`); make sure you ran the `download_models()` step. **Triggers too easily** → raise it. If it cuts you off mid-sentence, raise `AUTOBOT_END_SILENCE_MS`.
- **Model replies in another language** → it shouldn't; STT and the system prompt are pinned to English. Treat it as a model-quality signal when A/B-ing.
