# Design reference

Durable design reference for Jack (the `autobot` package): the interface
boundaries, the tech-stack choices and *why*, and the hardware tiers. This is
**reference, not tracking** — it describes how the system is built, not what's
planned.

> **Planning, feature requests, and status live in GitHub** — see
> [Project #1 "Jack Assistent"](https://github.com/users/mdjakkariya/projects/1)
> and the repo's Issues. Do not record plans or status here.

For *how we build* (conventions, the non-negotiable constraints), see
[`CLAUDE.md`](../../CLAUDE.md). For the rendered architecture, see
[`architecture.svg`](architecture.svg).

---

## Interface Contracts

Each component sits behind a stable interface (a `Protocol` in
`src/autobot/core/interfaces.py`) so models and back-ends are swappable per
hardware tier as a config change, not a rewrite.

| Component | Input | Output |
|-----------|-------|--------|
| STT | audio clip (English) | `(text, confidence)` |
| LLM | `(text, context)` | intent / tool-calls |
| TTS | text | audio |
| Memory | query | context |
| Tool (via MCP) | structured args | result + status |

---

## Tech Stack

| Concern | Choice | Why |
|---------|--------|-----|
| Orchestration | Python 3.11+, asyncio | Deepest AI ecosystem; event loop fits listen/callback model. Inference runs in native runtimes anyway, so Python speed is irrelevant at runtime. |
| IPC / daemon | FastAPI + localhost WebSocket | Streams state/partials to any UI client. Drop to Unix socket / ZeroMQ later only if measured. |
| Mic capture | sounddevice (PortAudio) | Ring-buffer capture |
| Wake word | openWakeWord (ONNX) | Free, trainable custom phrase, runs real-time even on a Pi |
| VAD | silero-vad | Tiny, reliable endpointing |
| STT | Moonshine (English) primary; faster-whisper `*.en` fallback | English-only, so prefer an English-optimized model: Moonshine is built for real-time English on constrained hardware, low-hallucination, ~27MB tiny / 245M base, ONNX/pip-installable. Use Parakeet V3 (NeMo) on NVIDIA GPUs for top English accuracy. faster-whisper with `.en` weights (or whisper.cpp on Apple Silicon) is the fallback when you want the deepest ecosystem. No multilingual builds. |
| LLM serving | Ollama | Handles download, quantization, hot-swap; OpenAI-compatible tool-calling API decouples model from app |
| TTS | Piper (CPU) / Kokoro (GPU) — English voices | Fast tier vs quality tier. Load only English voice models. |
| Action layer | MCP servers (Python MCP SDK) | Standard, swappable tool interface the agent ecosystem is converging on |
| Permission gate | Custom policy layer + SQLite audit | Risk classification, confirmation, sandbox, audit trail |
| Memory | SQLite + sqlite-vec | One local file, zero extra services — cleanest privacy story |
| Desktop UI | Tauri (Rust + web) | Tiny binary vs Electron; talks to same daemon |

**On Rust:** the only hot path where Python's GIL/latency realistically bites is
the always-on audio + wake-word loop. openWakeWord runs many models in real time
even on a Pi 3, so don't pre-optimize. If you later *measure* audio jitter, move
only that capture loop to Rust; leave orchestration in Python. A full Rust rewrite
costs weeks of velocity for near-zero runtime gain, since inference is already
native.

---

## Hardware Tiers

| Tier | Hardware | STT | LLM | TTS |
|------|----------|-----|-----|-----|
| Low | CPU-only, ≤8GB RAM, Pi | Moonshine tiny/base | Phi-4 / small Qwen (Q4) | Piper (EN) |
| Mid | 16GB RAM, Apple Silicon, modest GPU | Moonshine base, or faster-whisper `small.en`/`medium.en` | Gemma 4 9B / Qwen ~8B | Piper (EN) |
| High | NVIDIA GPU 12GB+ | Parakeet V3 (via NeMo) or faster-whisper `large-v3` (EN) | Qwen 3.x 32B / Gemma 4 26B | Kokoro (EN) |

> STT note: this assistant is **English-only**, and you transcribe short command
> clips bounded by VAD, not a live stream — so optimize for accuracy on short
> English utterances, hallucination robustness, and footprint, not language
> coverage. That makes the English-first models the right default rather than a
> compromise: **Moonshine** (efficient, low-hallucination, English MIT-licensed,
> ~245M base) for the low/mid tiers, **Parakeet V3** (strong English accuracy,
> Apache 2.0, NeMo setup overhead) on capable NVIDIA GPUs. faster-whisper `*.en`
> is the fallback; avoid multilingual Whisper builds entirely — they carry the
> silence-hallucination risk and weight you don't need.

The current dev machine (MacBook Air M2, 16 GB, macOS 15) is the **Mid** tier.
Defaults: `qwen3:8b` (LLM), `small.en` (STT). See `CLAUDE.md` → "Target hardware"
for the STT engine details (faster-whisper vs whisper.cpp).

---

## UI architecture (the webview frontend)

The UIs (orb, chat drawer, settings, about) are thin clients of the daemon. They
live under `ui/orb/` and are loaded **buildless** by Tauri (`frontendDist` points at
the raw folder; no bundler, no npm at runtime). The architecture, settled in the
2026-06 refactor (spec + plan under `docs/superpowers/`), is:

- **Each `*.html` is a thin shell** — body markup + one `<link>` to a per-page
  stylesheet + one `<script type="module">` entry. No inline `<style>`/`<script>`.
- **`ui/orb/lib/`** — shared, framework-free ES modules: `daemon.js` (the single
  auto-reconnecting WebSocket + typed fetch client — the one place the daemon URL
  and endpoints are named), `tauri.js` (bridge wrappers), `clipboard.js`,
  `earcons.js` (WebAudio cues), `markdown.js` + `format.js` (pure), `dom.js`,
  `orb-renderer.js` (WebGL orb).
- **`ui/orb/components/<name>/`** — **light-DOM web components** (no Shadow DOM, so
  shared CSS tokens reach in) that *enhance existing markup*, each with co-located
  `<name>.css` and `<name>.test.js`. Pieces whose DOM is created dynamically
  (chat confirm/choices cards) or spread across non-adjacent elements
  (context-meter, folder-chip, report-sheet) are plain controller **modules**
  instead of custom elements — that's the documented exception, not the rule.
- **`ui/orb/pages/`** — one entry module per document; wires components + page glue.
- **`ui/orb/styles/`** — one `tokens.css` (tiered primitive→semantic custom
  properties + dark mode) `@import`ed by every page; `@layer reset, base,
  components, utilities`. A surface that diverges (e.g. the chat drawer's
  translucent bg) overrides only those tokens, **unlayered and after the import**
  (tokens.css is imported unlayered, so it would otherwise win).
- **Imports are relative ES-module paths**; no import map (only four shallow pages).
- **Testing is dev-only** (`ui/package.json`, `make ui-test`): Vitest + happy-dom,
  tests co-located as `*.test.js`. Nothing about what ships changes. The real macOS
  WKWebView has no automation driver, so final visual/behavioral parity is verified
  manually via `make run`.

Why this shape: it kills the duplication of the old monolithic-IIFE-per-page files
(design tokens, earcons, WS loops, `copyText`, the Tauri accessor, fetch boilerplate
all existed 2–3×), keeps files small and single-purpose (good for humans and for an
LLM working on one component without loading the whole codebase), runs under the
strict CSP (no `eval`, no CDN — which rules out Alpine/petite-vue/htmx-eval), and
adds no runtime dependency, matching the project's on-device, minimal-dependency
ethos. See `docs/superpowers/specs/2026-06-28-ui-architecture-design.md` for the
research basis (GitHub Catalyst, GOV.UK Frontend, the "HTML web components" canon).

## Async tasks (background execution → multi-agent)

Work that runs *off* a turn goes through one primitive in `src/autobot/tasks/`, so the
same shape serves two features: a backgrounded command today (`kind="command"`) and,
later, concurrent subagents (`kind="agent"`). Both reduce to *a unit of work that runs off
the main turn and whose completion is delivered back as a notification that re-engages the
agent* — build it once, add the second kind later without a rewrite.

Two decoupled pieces:

- **`TaskRegistry`** — a thread-safe, process-global store of task rows
  (`running → done/failed`). It lives in the daemon, so a task started in one turn is still
  tracked in the next. It only records state; it never spawns work or decides who is told.
- **`NotificationInbox`** — a per-session FIFO of completion notes (plain strings, so it
  carries a command result now and a subagent's return later the same way).

Flow for a backgrounded `run_command(run_in_background=True)`: the tool reads the running
session id from the `active_session_id` context var, registers a `command` task, and spawns
a daemon thread that streams output to `<cwd>/.jack/tasks/<id>.log`. When the process exits,
that thread marks the registry and pushes a note to the session's inbox. `AgentHarness`
drains its session's inbox at the top of every turn and folds any notes into the model's
context — so the result arrives on the **next turn** with no polling. This keeps the
no-sleep-poll guidance honest: the model is *told* to background long work and *told* the
result later, rather than blocking or re-checking.

Not yet built (future phases): **auto-resume** (a persistent `GET /coder/events` stream +
a daemon-initiated continuation turn, so a result surfaces while the user is idle instead of
on their next message) and **subagents** (`kind="agent"` = an `AgentHarness` on its own
`Session` with a scoped broker, fanned out by a coordinator). The registry/inbox are already
kind-agnostic, so those add on top rather than replacing this.

## Reference projects to study

- **Home Assistant voice pipeline + Wyoming protocol** — an existing open standard
  for chaining wake-word / STT / TTS as interchangeable services. Its interface
  boundaries are a proven reference for ours.
- **Model Context Protocol (MCP)** — the tool/action interface standard for the
  permission-gated action layer.
- **GitHub Catalyst + `@github/*` elements, GOV.UK Frontend** — references for the
  buildless light-DOM web-component frontend pattern (one folder per component,
  attribute-driven enhancement, co-located tests).
