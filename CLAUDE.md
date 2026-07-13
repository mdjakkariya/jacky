# CLAUDE.md

Guidelines for AI assistants (and humans) working in this repo — the single source
of truth for *how* we build here. (For *what's planned and its status*, see
**GitHub Issues + Project #1**, never markdown; this file holds no status.)

## What this is

**Jack** — a local, privacy-first, **English-only** macOS voice assistant. (The
codebase and Python package are named `autobot`; "Jack" is the product the user
sees.) Everything runs on-device; no audio, text, or memory leaves the machine
except the two disclosed, opt-in exceptions below.

The product is a floating **orb** plus a right-docked **chat drawer** (a Tauri shell
over a system webview) that are thin clients of a **headless Python daemon**. **Chat
is the default**; voice is opt-in, and the speech models download on demand the first
time it's enabled (they're not bundled). The LLM is local (Ollama) by default, with
an optional cloud provider (Anthropic).

- Planning & tracking: **GitHub Issues + [Project #1](https://github.com/users/mdjakkariya/projects/1)** (see "Planning & tracking" below).
- Architecture diagram: `docs/architecture/architecture.svg`; design reference: `docs/architecture/design-reference.md`.

## Planning & tracking

Planning, feature requests, and status live in **GitHub Issues + Project #1**
("Jack Assistent"), never in markdown. Do **not** create or edit tracking markdown
(roadmaps, TODO docs, status checklists, "next steps" files) — that's what GitHub is
for. To propose or plan work, open an issue (use the *Feature request* or *Task*
template); it lands on the board automatically. To record *how something works*, add
to `docs/` (durable design reference: `docs/architecture/design-reference.md`). Every
PR links its issue with `Closes #NN`.

## Non-negotiable constraints

1. **On-device only.** Never add a dependency or call that sends audio, text, or
   user data off the machine. This is the entire point of the project. **The one
   sanctioned exceptions are **opt-in, off by default, and disclosed**:
   (a) the `web_search` tool (`tools/web.py`) sends only the search *query* and is
   only registered when `allow_web` is set; and (b) the **optional cloud LLM**
   (`llm/anthropic_llm.py`, `llm_provider="anthropic"`) sends the conversation +
   memory profile + tool schemas/results to Anthropic — but never audio, and
   never the *actions* (those still run locally through the permission gate). Any
   other off-device feature needs the same explicit, opt-in, disclosed treatment.
   Secrets (API keys) live in the macOS Keychain (`autobot.secrets`), never on disk.
2. **English only**, both directions (STT and TTS). Prefer English-optimized
   models (Moonshine, Parakeet, `*.en` whisper builds). Do not reintroduce
   multilingual options.
3. **The permission gate is not optional.** Any genuinely-acting tool (write,
   delete, network, shell) must go through the registry's risk classification and
   the permission gate. Never let the LLM execute side effects unguarded.
4. **Engine stays headless.** UIs (orb, chat drawer) are thin clients of the daemon
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
docs/          architecture + design reference (no tracking — see GitHub Issues/Project)
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
- **Prompt vs. tool descriptions.** Keep `SYSTEM_PROMPT` (`llm/ollama_llm.py`) to
  short, *general* behavioral principles. Per-tool guidance — when to use a tool
  and which spoken words map to it (synonyms) — lives in that tool's
  `ToolSpec.description`, next to the tool. So adding/teaching a tool never edits
  the global prompt. When the model misbehaves, fix the *general* principle (one
  rule that covers a whole class) or the relevant tool description — never append
  an incident-specific line to the prompt.
- **Commit messages: Conventional Commits.** Write `feat: …`, `fix: …`, `perf: …`,
  `refactor: …`, `docs: …`, `chore: …` (use `!` or a `BREAKING CHANGE:` footer for
  breaking changes; `chore(release): vX` for release bumps). The changelog and
  GitHub release notes are generated from these by git-cliff. Non-conventional
  commits are left out of the changelog, so keep the subject in this form.
  **Scope tells the two changelogs apart:** scope orb-only work `(orb)`/`(ui)`;
  everything else (`cli`, `engine`, `update`, `install`, `daemon`, or unscoped) is the
  CLI/engine track. `git-cliff` writes `CHANGELOG-cli.md` (tag `vX.Y.Z`) and
  `CHANGELOG-orb.md` (tag `orb-vX.Y.Z`) from those scopes — see
  [`docs/reference/RELEASING.md`](docs/reference/RELEASING.md).

## Commands

```bash
make setup      # create env (uv sync) + install pre-commit hooks
make check      # lint + format-check + mypy + tests  (run before every commit)
make test       # pytest with coverage
make run        # launch the assistant (Ollama must be running)
```

Configuration is a single persisted JSON file `~/.autobot/settings.json` —
**no environment variables**. `Settings.load()` overlays it on the dataclass
defaults (`settings.json > defaults`); the Settings view (via the daemon) writes
it. Secrets (API keys) are **never** in this file — they live in the macOS
Keychain via `autobot.secrets` (`get_secret`/`set_secret`). To change a tunable,
edit `settings.json` (or use the Settings view); to add one, add a dataclass
field with a default in `config.py` (that's the single source).

Input mode is the `input_mode` setting: `wake` (default, hands-free) or `ptt`.
Hands-free needs the optional wake deps: `uv sync --extra wake` (openWakeWord +
onnxruntime; the VAD runs silero's vendored ONNX model directly — no torch). Wake/VAD model wrappers and
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
`small.en` (STT). The default STT engine is faster-whisper (CTranslate2,
CPU/int8 — no Metal backend). For GPU-accelerated STT on Apple Silicon, set
`stt_engine="whisper_cpp"` (or pick it in the Settings view) — that path uses
whisper.cpp with Metal and can run `medium.en`/`large-v3` far faster. It's an
opt-in extra (`uv sync --extra whispercpp`), lazy-imported, and falls back to
faster-whisper if the extra is missing. Both engines stay behind the
`SpeechToText` protocol and are hot-swappable via the reloadable STT proxy.
