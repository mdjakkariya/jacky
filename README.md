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

2. **Start Ollama and pull the model** (`qwen3:8b` is the default — best small tool-caller for 16 GB):

   ```bash
   ollama serve            # leave running in its own terminal tab
   ollama pull qwen3:8b    # ~5 GB, one time
   ```

3. **Create the dev environment** (installs deps + git hooks):

   ```bash
   cd /path/to/autobot
   make setup
   ```

4. **For hands-free mode** (default), install the optional wake/VAD models and download the wake-word weights:

   ```bash
   uv sync --extra wake          # openWakeWord + silero-vad (heavy: pulls onnxruntime + torch)
   uv run python -c "import openwakeword.utils as u; u.download_models()"
   ```

   Prefer to skip this for now? Run push-to-talk instead with `AUTOBOT_INPUT=ptt` (no extra needed).

5. **Microphone permission:** the first run prompts macOS to let your terminal use the mic — allow it (System Settings → Privacy & Security → Microphone). The first run also downloads the `base.en` whisper weights (~150 MB).

---

## Run

```bash
make run            # or: uv run autobot   /   uv run python -m autobot
```

**Hands-free (default):** say the wake word **"hey jarvis"**, then speak your command — no keypress. VAD detects when you stop and cuts the clip. You'll see live `[state]` transitions, the transcription, and the reply.

**Push-to-talk:** `AUTOBOT_INPUT=ptt make run` — press Enter to start/stop recording instead.

Either way, try:

- *"what time is it"* — read-only tool, runs straight through.
- *"create a file called notes.txt that says hello"* — a WRITE action; runs and is audited.
- *"delete notes.txt"* — a DESTRUCTIVE action; prompts `⚠ … Proceed? [y/N]` and only runs on `y`.

Acting tools are confined to the workspace dir (`~/.autobot/workspace` by default) and every attempt is recorded in the audit log (`~/.autobot/audit.db`). Override with `AUTOBOT_SANDBOX_DIR` / `AUTOBOT_AUDIT_DB`.

### Wake word

The default phrase is the pretrained **"hey jarvis"** model. To use a different built-in (e.g. `alexa`, `hey_mycroft`): `AUTOBOT_WAKE_MODEL=alexa make run`. A custom **"Autobot"** phrase requires training your own openWakeWord model offline (see [openWakeWord training docs](https://github.com/dscripka/openWakeWord)) and pointing `AUTOBOT_WAKE_MODEL` at it. Tune sensitivity with `AUTOBOT_WAKE_THRESHOLD` / `AUTOBOT_VAD_THRESHOLD` / `AUTOBOT_END_SILENCE_MS`.

### Try a different model (no code changes)

```bash
AUTOBOT_LLM_MODEL=qwen3:4b make run    # snappier; tighter leash needed
AUTOBOT_LLM_MODEL=gemma4:2b make run   # very constrained hardware
```

Remember to `ollama pull <model>` first. All tunables live in `src/autobot/config.py` (env vars: `AUTOBOT_LLM_MODEL`, `AUTOBOT_STT_MODEL`, `AUTOBOT_LLM_TEMPERATURE`, `OLLAMA_HOST`, …).

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

## Troubleshooting

- **`ConnectionError` / model not found** → ensure `ollama serve` is running and you've `ollama pull`ed the model named in `AUTOBOT_LLM_MODEL`.
- **No audio / `PortAudioError`** → confirm the terminal has mic permission. If it persists (rare), `brew install portaudio` then `uv sync --reinstall-package sounddevice`.
- **`Hands-free mode needs the 'wake' extra`** → run `uv sync --extra wake`, or use `AUTOBOT_INPUT=ptt` for push-to-talk.
- **Wake word never triggers** → lower `AUTOBOT_WAKE_THRESHOLD` (e.g. `0.3`); make sure you ran the `download_models()` step. **Triggers too easily** → raise it. If it cuts you off mid-sentence, raise `AUTOBOT_END_SILENCE_MS`.
- **Model replies in another language** → it shouldn't; STT and the system prompt are pinned to English. Treat it as a model-quality signal when A/B-ing.
