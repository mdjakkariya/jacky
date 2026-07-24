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
context — so the result is available on the next turn with no polling. This keeps the
no-sleep-poll guidance honest: the model is *told* to background long work and *told* the
result later, rather than blocking or re-checking.

**Auto-resume** closes the loop so the result surfaces even while the user is idle, not just
on their next message. The registry has an `add_listener` seam that fires when a task
settles; the daemon exposes those as a persistent `GET /coder/events` SSE
(`Orchestrator.subscribe_coder_events`). The CLI keeps that stream open on a background
thread (`cli/autoresume.py`); when a task finishes while the prompt is idle it wakes the
prompt (the auto-reader returns an `AUTO_CONTINUE` sentinel) and the shell runs a
continuation turn on its own — reusing the normal `/coder/turn` path, so the harness folds
the result in exactly as above. It never interrupts mid-typing: the waker fires only on an
empty, running prompt; otherwise the result waits for the next real turn.

**Subagents** are the `kind="agent"` instance of the same primitive (`agent/subagent.py`).
The coordinator's `spawn_agent` tool fans out focused, READ-ONLY research agents that run in
parallel: each is a fresh, *isolated* `AgentHarness` (its own model + `Session` — the model
adapters keep per-turn state, so a subagent can never share the coordinator's — with a
tighter turn budget), driven through `subagent_executor`, which refuses anything at or above
`Risk.WRITE` and refuses `spawn_agent` itself (so a subagent can't mutate the workspace or
recurse). A subagent runs as an `agent` task off the turn; on completion its findings are
pushed to the *parent* session's inbox, so the coordinator picks them up through the same
auto-resume path — no polling. Concurrency is capped; per-agent cost falls out of the usage
ledger's `session_id` tagging. Safety composes: a subagent is never more privileged than the
coordinator, and (being read-only) it can't spawn processes, so there are no orphaned tasks
to reap. Write-capable subagents (git-worktree isolation for parallel edits) are a future
extension; today the coordinator does the edits itself using subagents' findings.

## MCP

The action layer's Model Context Protocol support (`src/autobot/mcp/`): a `McpManager`
runs every configured server's `McpServerWorker` on one dedicated asyncio event-loop
thread — one loop for all servers, driven synchronously from the daemon and composition
root via `run_coroutine_threadsafe` — and registers each server's tools into the shared
`ToolRegistry` under a `<server_id>__<tool_name>` namespace so they flow through the same
permission gate as built-in tools. Stdio and remote (HTTP/SSE) transports are both
supported; OAuth for remote servers and Keychain-backed secrets for stdio env vars are
handled per server. A rug-pull guard fingerprints each tool's schema at approval time and
re-blocks it for re-consent if a reconnect changes that fingerprint.

### MCP in the CLI (coder profile)

The coder daemon serves the same `/mcp/*` routes the orb uses; the CLI is a thin
client of them from two surfaces backed by one client layer (`cli/mcp_client.py`)
and one renderer (`cli/mcp_render.py`):

- **REPL:** `/mcp` (list · add wizard · enable/disable · remove · tools · tool
  risk/on/off · auth · consent · on/off) — interactive steps ride the pinned-input
  modal (`Segment` kinds `input` and `secret`; secrets are masked and never enter
  the input history).
- **Shell:** `jack mcp …` — same verbs with flags, `--yes` for non-interactive
  consent (CI), `--json` for raw payloads. Auto-starts the workspace daemon.

**Consent-at-enable.** In the coder profile the MCP manager runs with
`consent="explicit"`: an enabled-but-unapproved stdio server parks in
`pending_consent` (nothing spawns at daemon startup, where no turn exists to
ask through). `POST /mcp/servers/{id}/enable` reports the pending command+args;
the CLI shows them and, on approval, `POST /mcp/servers/{id}/consent`
(`McpManager.grant_consent`) records the spawn approval and connects. The same
endpoint clears rug-pull re-consent blocks by dropping the blocked tools'
approved fingerprints so the reconnect re-baselines them. The assistant profile
is unchanged (`consent="confirmer"` asks through the gate's confirmer).

**Live feedback.** The daemon fans every `mcp_status`/`mcp_oauth` event to the
orb bus *and* a `CoderEventHub` (`daemon/coder_events.py`) tapped by each
`/coder/events` SSE subscription, so the REPL shows connect/OAuth progress lines
as they happen (`cli/autoresume.py` → `JackApp.on_mcp_event`).

## CLI HUD (docked status bar)

The CLI's bottom status bar is a live, segment-based HUD (`cli/hud/`). `HudState`
(`state.py`) is a mutable snapshot; pure segment renderers (`segments.py`) turn it
into `(style, text)` fragments, each with a drop-priority; `compose.py` resolves
settings into ordered rows and width-gates each row (dropping the lowest-priority
segments until it fits). `JackApp._status_text` delegates to `compose`; the status
window is one line (minimal/essential) or two (full).

**Feeds.** Startup `gather_context` seeds autonomy/model/provider/git/cwd + a
best-effort MCP count. The context-window %, tokens, and session cost are pulled at
**turn-end** from `GET /coder/usage` (`_refresh_hud_after_turn` in `cli/shell.py`):
its `ctx` block carries the live `used`/`window`/`model` (the same payload the orb
meter uses) and `rollups.session.usd` the cost. Polling — not the bus `ContextEvent`
— is deliberate: that event only reaches the orb's WebSocket `/events` channel, and
context usage only changes at turn boundaries, so a turn-end pull is both correct
and simpler than a live event the CLI never receives.

**Customization (all `settings.json`).** `hud_enabled` (off → the minimal static
line), `hud_preset` (`minimal`/`essential`/`full`), `hud_segments` (which segments +
order + on/off; overrides the preset), `hud_options` (per-segment label/flags),
`hud_separator`, and `hud_context_warn`/`hud_context_crit` (bar color thresholds).
`hud.resolve_config` validates a hand-edited file (unknown segment/preset ignored),
mirroring config's never-crash-on-bad-input rule.

## Reference projects to study

- **Home Assistant voice pipeline + Wyoming protocol** — an existing open standard
  for chaining wake-word / STT / TTS as interchangeable services. Its interface
  boundaries are a proven reference for ours.
- **Model Context Protocol (MCP)** — the tool/action interface standard for the
  permission-gated action layer.
- **GitHub Catalyst + `@github/*` elements, GOV.UK Frontend** — references for the
  buildless light-DOM web-component frontend pattern (one folder per component,
  attribute-driven enhancement, co-located tests).
