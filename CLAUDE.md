# CLAUDE.md

Instructions for AI assistants (and humans) working in this repo. Keep this file
updated as the project grows — it is the single source of truth for *how* we build.

## What this is

**Autobot** — a local, privacy-first, **English-only** voice assistant (Jarvis-style).
Everything runs on-device; no audio, text, or memory ever leaves the machine.

- Full build plan: `docs/plans/autobot_build_roadmap.md` (6 risk-ordered phases).
- Architecture diagram: `docs/architecture/`.
- **Current status: Phase 3a complete** (voice output via Piper TTS, behind a
  swappable `TextToSpeech` interface). On top of Phase 2 (wake word + VAD), Phase 1
  (orchestrator + permission gate), Phase 0 spine. Next: Phase 3b — headless
  daemon + Textual terminal UI.

## Non-negotiable constraints

1. **On-device only.** Never add a dependency or call that sends audio, text, or
   user data off the machine. This is the entire point of the project. **The one
   sanctioned exception is the `web_search` tool** (`tools/web.py`): it sends only
   the search *query* to a search engine, is **opt-in** (`AUTOBOT_ALLOW_WEB`, off
   by default; the tool isn't even registered otherwise), and every call is
   audited. Any other off-device feature needs the same explicit, opt-in, audited
   treatment.
2. **English only**, both directions (STT and TTS). Prefer English-optimized
   models (Moonshine, Parakeet, `*.en` whisper builds). Do not reintroduce
   multilingual options.
3. **The permission gate is not optional.** Any genuinely-acting tool (write,
   delete, network, shell) must go through the registry's risk classification and
   the Phase 1 gate. Never let the LLM execute side effects unguarded.
4. **Engine stays headless** (from Phase 3 on). UIs are thin clients of a daemon
   API; never build the assistant *as* a UI app.

## Architecture in one paragraph

The pipeline is a sequence of swappable stages defined as `Protocol`s in
`src/autobot/core/interfaces.py`: `AudioSource → SpeechToText → LanguageModel`.
The `Orchestrator` (`orchestrator/state_machine.py`) drives one turn through an
explicit `State` machine and hands the `LanguageModel` an **executor** — a
callback wired to the `PermissionGate`. So the model plans tool calls but never
runs them itself; execution flows model → executor → gate → `ToolRegistry`. The
gate classifies risk, confirms destructive actions, and writes every decision to
the SQLite audit log; the `Sandbox` path-jails all filesystem tools. Concrete
implementations live in sibling subpackages (`io/`, `stt/`, `llm/`, `tools/`,
`orchestrator/`), wired together in the composition root,
`src/autobot/app.py::build()` — **the only place** that names concrete classes.
Everything else depends on the protocols, so swapping a model, back-end, or
policy is a one-line change in `build()` and nowhere else.

## Layout

```
src/autobot/
  core/        interfaces.py (Protocols) + types.py (value objects, Risk enum)
  config.py    Settings dataclass; the ONLY place env vars are read
  io/          audio capture: push-to-talk + wake-word/VAD (TTS later)
  stt/         speech-to-text engines (English-only)
  llm/         Ollama tool-calling client + pure parsing helpers
  tools/       registry, permission gate, sandbox, audit log, built-in + fs tools
  orchestrator/ state machine + turn loop (the backbone)
  app.py       composition root + the run loop
  __main__.py  enables `python -m autobot`
tests/unit/    fast tests that need no model runtime or microphone
docs/          roadmap + architecture
```

## How to add a component (the pattern)

1. Add or reuse a `Protocol` in `core/interfaces.py`.
2. Implement a concrete class in the right subpackage. **Import heavy runtimes
   lazily** (inside `__init__`/methods), so importing the module — and thus the
   test suite — stays fast and dependency-free.
3. Wire it in `app.py::build()`.
4. Add unit tests for any pure logic (parsing, dispatch, config). Keep model- and
   mic-dependent paths out of unit tests.

## Conventions

- Python ≥ 3.11, `from __future__ import annotations` in every module.
- Full type hints; **mypy runs in `strict` mode** — keep it green.
- Google-style docstrings on public modules, classes, and functions
  (enforced by ruff's pydocstyle `D` rules; tests are exempt).
- Line length 100. Formatting and import order are owned by `ruff` — don't
  hand-format; run `make format`.
- Value objects are `frozen=True, slots=True` dataclasses. No business logic on them.
- Tools return strings and never raise out of `dispatch`; errors become failed
  `ToolResult`s so a bad tool can't crash the loop.

## Commands

```bash
make setup      # create env (uv sync) + install pre-commit hooks
make check      # lint + format-check + mypy + tests  (run before every commit)
make test       # pytest with coverage
make run        # launch the assistant (Ollama must be running)
```

The model is configurable without code changes, e.g.
`AUTOBOT_LLM_MODEL=qwen3:4b make run`. All env vars live in `config.py`.

Input mode is `AUTOBOT_INPUT=wake` (default, hands-free) or `ptt` (push-to-talk).
Hands-free needs the optional wake deps: `uv sync --extra wake` (openWakeWord +
silero-vad; heavy, so kept out of the core install). Wake/VAD model wrappers and
the mic are injected into `WakeWordVadRecorder`, and the endpointing/pre-roll
logic is pure — so the real-time loop is unit-tested without hardware.

## Logging (add logs as you build)

A single rotating debug log lives at `~/.autobot/logs/autobot.log` (DEBUG; console
shows WARNING+ only, so normal runs stay clean). It's meant to be **shared as-is**
when reporting a bug, so keep it **signal, not noise**.

Rules:

- Get a logger per component: `from autobot.logging_setup import get_logger` then
  `_log = get_logger("stt")` (module level). The component name becomes the
  `[stt]` tag on every line — the handle used to **filter**:
  `make logs-grep C=stt` (or `grep '\[stt\]' ~/.autobot/logs/autobot.log`).
  Existing tags: `app`, `orchestrator`, `gate`, `stt`, `llm`, `listening`, `wake`.
- Log **events at the seams**, never inside hot loops (no per-frame/per-token logs).
- Use `key=value` properties so lines are readable and greppable, e.g.
  `_log.info("captured seconds=%.1f frames=%d", s, n)`. Pass args to the logger
  (`%`-style), don't f-string them in.
- Levels: `DEBUG` = detail (state transitions, tool args, per-call timing);
  `INFO` = lifecycle/seam events (startup, transcript, tool decisions, replies);
  `WARNING` = recoverable problems; `ERROR`/`_log.exception(...)` = failures
  **with traceback** (the run loop already does this for uncaught turn errors).
- Only the `autobot.*` logger is wired (`propagate=False`), so third-party
  libraries never pollute the file. Don't add handlers elsewhere.
- **When you add a feature, add its logs** (a component logger + seam events) as
  part of the change — same as adding tests.

## Verification expectations

Before considering any change done: `make check` must pass (ruff, ruff-format,
mypy strict, pytest). Add tests with new logic. CI (`.github/workflows/ci.yml`)
runs the same checks once the repo is on GitHub.

## Target hardware (current dev machine)

MacBook Air M2, 16 GB, macOS 15 → "Mid" tier. Defaults: `qwen3:8b` (LLM),
`base.en` (STT, CPU/int8 — CTranslate2 has no Metal backend).
