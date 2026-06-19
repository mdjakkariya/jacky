# Autobot

A privacy-first, zero-cost, **on-device** voice assistant. Everything runs locally — no audio, text, or memory leaves your machine. Build sequence and rationale live in [`docs/plans/autobot_build_roadmap.md`](docs/plans/autobot_build_roadmap.md); engineering conventions live in [`CLAUDE.md`](CLAUDE.md).

**Constraint:** English only, both directions (STT and TTS).

**Status:** Phase 1 complete — orchestrator state machine + sandboxed, audited, permission-gated filesystem tools (on the Phase 0 push-to-talk spine).

---

## Architecture

A pipeline of swappable stages, each defined as a `Protocol` in `src/autobot/core/interfaces.py`, driven by an orchestrator state machine. The language model **plans** tool calls; the **permission gate** executes them:

```
Orchestrator (state machine: idle→listening→transcribing→planning→executing→responding)
  AudioSource ─▶ SpeechToText ─▶ LanguageModel ─plan─▶ PermissionGate ─▶ ToolRegistry
   (mic)          (base.en)       (Ollama)             (risk? confirm?      (get_time,
                                                        audit, sandbox)      create/move/delete)
```

The model never runs tools directly — it calls an executor the orchestrator wires to the gate. Concrete implementations live in `io/`, `stt/`, `llm/`, `tools/`, `orchestrator/` and are wired together in one place — `src/autobot/app.py::build()`. Swapping a model, back-end, or policy is a one-line change there.

```
src/autobot/
  core/         interfaces (Protocols) + value types + Risk/State/Decision enums
  config.py     typed Settings; the only place env vars are read
  io/           microphone capture (Phase 2: wake word + VAD)
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

4. **Microphone permission:** the first run prompts macOS to let your terminal use the mic — allow it (System Settings → Privacy & Security → Microphone). The first run also downloads the `base.en` whisper weights (~150 MB).

---

## Run

```bash
make run            # or: uv run autobot   /   uv run python -m autobot
```

Press Enter to start recording, speak a command, press Enter to stop. You'll see the live `[state]` transitions, the transcription, and the reply. Try:

- *"what time is it"* — read-only tool, runs straight through.
- *"create a file called notes.txt that says hello"* — a WRITE action; runs and is audited.
- *"delete notes.txt"* — a DESTRUCTIVE action; prompts `⚠ … Proceed? [y/N]` and only runs on `y`.

Acting tools are confined to the workspace dir (`~/.autobot/workspace` by default) and every attempt is recorded in the audit log (`~/.autobot/audit.db`). Override with `AUTOBOT_SANDBOX_DIR` / `AUTOBOT_AUDIT_DB`.

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
- **Model replies in another language** → it shouldn't; STT and the system prompt are pinned to English. Treat it as a model-quality signal when A/B-ing.
