# Design: Jack as a coding agent (headless harness, any-LLM, CLI + chat)

- **Date:** 2026-07-03
- **Status:** Approved (design); scoped to v1 = Phases 1–2
- **Prototype:** interactive UI/UX prototype (Artifact) — surfaces, plan→approve→act loop, use cases, architecture, roadmap: https://claude.ai/code/artifact/1c895c06-1913-47ee-806d-adb2ab606f04
- **Tracking:** GitHub Issues + Project #1 — **Epic #56**; Phase 1 = #44–#47, Phase 2 = #48–#53, Phase 3 = #54, Phase 4 = #55. This document is durable *design reference*, not a status tracker — status lives in GitHub.

## 1. Context

Jack (package `autobot`) is a local-first macOS voice assistant built as a headless
Python daemon with thin UI clients (orb + chat drawer). We are extending it into a
**coding agent** (Cline/Copilot-class) that plans, edits, and runs code — drivable
from a terminal CLI *and* the existing chat drawer, and later from IDEs.

The existing architecture already provides ~70% of a coding-agent harness:

| Coding-agent need | Already present | Location |
|---|---|---|
| Multi-round agentic tool loop | ✅ (but duplicated per provider) | `llm/ollama_llm.py::run_turn`, `llm/anthropic_llm.py` |
| Permission/approval middleware | ✅ `PermissionGate` | `tools/permission.py` |
| Path-jail / workspace sandbox | ✅ `AccessPolicy` (deny-by-default) | `tools/access.py` |
| Append-only audit log | ✅ SQLite | `tools/audit.py` |
| Headless daemon + event protocol | ✅ FastAPI REST `/chat` + WebSocket `/ws` | `daemon/server.py` |
| Context compaction | ✅ threshold summarize | `llm/ollama_llm.py` |
| Cloud + local LLM behind one seam | ✅ Ollama + Anthropic | `llm/` |
| Tool registry + risk classification | ✅ `ToolSpec`, `ToolRegistry` | `tools/registry.py` |

The gaps this design fills: a **reusable agent harness** (the loop is currently
duplicated inside each provider), a **provider-agnostic model layer** ("any LLM via
API key"), **code-editing/navigation/execution tools**, a **repo map**, **plan
mode + checkpoints**, a **coding-focused security gate**, and two new surfaces
(a **cross-platform CLI** and **coding UX in the chat drawer**).

## 2. Goals & non-goals

**Goals (v1):**
- Extract a single reusable `AgentHarness` used by both the voice assistant and the
  coding agent (one engine, two profiles).
- Provider-agnostic model layer: any LLM via API key (OpenAI-compatible + native
  adapters), keys in a cross-platform keyring.
- Core code tools (read/edit/write/multi-edit/grep/glob/run_command/git) that are
  **OS-neutral**, plus a tree-sitter repo map.
- Plan → approve → act autonomy with git-snapshot checkpoints and undo.
- A coding-focused security gate (secret redaction, command allow/blocklist, egress
  disclosure) layered on the existing gate/sandbox/audit.
- Drive coding from the existing chat drawer and a basic cross-platform `jack` CLI.

**Non-goals (v1) — deferred to later phases:**
- ACP bridge / IDE (Zed, JetBrains) integration (Phase 4).
- Subagents / multi-agent orchestration (Phase 4).
- LSP-backed symbol tools, code-focused MCP (Phase 4).
- Rich TUI (Textual) and polished per-hunk diff review UX (Phase 3).
- Multi-provider "architect/editor" split-model routing (later, optional).

## 3. Decisions (locked in brainstorming)

1. **LLM strategy — any LLM via API key, provider-agnostic**, with a strong security
   gate. Not Anthropic-only. This is the go-forward posture. Cloud LLM egress for the
   coding path is a *disclosed, gated* exception to the on-device default (like
   `web_search` already is); audio never leaves the device.
2. **Approach A — extract a reusable `AgentHarness`.** Lift the loop out of the
   provider classes; providers become thin `ChatModel` adapters. Coding vs. assistant
   are **profiles** over one harness.
3. **Harness lives in the headless Python core; the CLI is a Python thin client** to
   the warm daemon. Startup is fast because the daemon is already running.
4. **v1 = focused MVP** (Phases 1–2). Parity features are later epic phases.
5. **GUI assistant stays macOS-first; the CLI coding agent is cross-platform.** The
   `coder` profile imports nothing macOS-only.
6. **Autonomy = plan → approve → act**, edits auto-apply inside the workspace, with
   heavy investment in efficient, creative UX.

## 4. Architecture

### 4.1 The `AgentHarness` (new `src/autobot/agent/`)

The agent loop moves out of the provider classes into one place.

- **`agent/chat_model.py` — `ChatModel` Protocol** (the entire provider surface):
  ```
  send(messages, tools, *, stream=False) -> ChatResponse   # (text, tool_calls, usage)
  context_window: int
  ```
  No loop logic. Provider-agnostic; the harness owns orchestration.

- **`agent/harness.py` — `AgentHarness`**: one ReAct loop.
  `run_turn(session, user_text, execute) -> str`. Per iteration:
  1. pre-check + compact if near token budget (per-tool-result compaction: big
     shell/test output summarized),
  2. (optional) think,
  3. `model.send(messages, tools)`,
  4. parse tool calls; each dispatched through `execute` (→ `PermissionGate`) — the
     model never runs side effects itself,
  5. append results, inject decision-point reminders,
  6. terminate on final text with no tool calls, iteration cap, or **doom-loop
     detection** (identical repeated call).
  Keeps the existing anti-thrash `find_tools` discovery behavior.

- **`agent/session.py` — `Session`**: `id`, `cwd`, `profile`, message history +
  running summary (moved out of the LLM classes), persistent transcript (JSONL under
  `~/.autobot/sessions/<id>.jsonl`), checkpoint refs, and token/cost tracking.

- **`agent/profile.py` — `AgentProfile`**: `system_prompt + tool_preset + mode`
  defaults. Two built-ins:
  - `assistant` — voice + macOS tools (today's behavior),
  - `coder` — cross-platform code tools, plan-mode default, repo-map context.

- **`agent/providers/`** — thin `ChatModel` adapters (~100 LOC each):
  - `openai_compatible.py` — OpenAI, OpenRouter, Groq, Together, LM Studio, vLLM, and
    **Ollama's OpenAI endpoint** (one adapter, many providers),
  - `anthropic.py` — native (keeps prompt caching + tool-search behavior),
  - `gemini.py` — native.

**Migration:** `Orchestrator` calls `harness.run_turn(session, text, execute)` instead
of `llm.run_turn(...)`. `OllamaLanguageModel` / `AnthropicLanguageModel` are refactored
into `ChatModel` adapters (their loop bodies deleted, their `send`/parse/window logic
kept). The `PermissionGate` / `AccessPolicy` / `AuditLog` / `EventBus` seams are
untouched. `app.py::build()` wires a `ChatModel` (chosen by `default_provider`) + the
harness; it remains the only place naming concrete classes.

### 4.2 Code tools (new `src/autobot/tools/code/`, all OS-neutral)

Registered only for the `coder` profile; risk-classified like every tool.

- **Edit family:** `edit_file` (search/replace blocks with fuzzy multi-pass matching —
  the format research shows most reliable), `write_file` (create-only), `read_file`
  (line-numbered), `multi_edit`, optional `apply_patch` (unified diff).
- **Navigation:** `grep` (ripgrep if on PATH, Python `re` fallback), `glob` /
  `list_files`.
- **Execution:** `run_command` — cross-platform shell (sh/bash on Unix, platform shell
  on Windows) with timeout, output truncation, background-process auto-detection, and
  cwd jail.
- **Git:** helpers for checkpoints (snapshot / restore / diff) via a shadow ref.
- **Repo map (`tools/code/repomap.py`):** tree-sitter (lazy import) ranked
  signatures à la Aider, injected as `coder`-profile context; cached + self-healing on
  file changes. Heavy runtime imported lazily so the test suite stays fast.

### 4.3 Security gate (coding-focused, layered on the existing gate)

We now ship code (and potentially secrets) to third-party endpoints *and* run edits +
shell, so security is first-class:

1. **Secret redaction** — an outbound pass scans context (files, tool results) and
   redacts secrets before anything reaches an LLM; extends the deny-list already in
   `tools/access.py` (`.env`, `id_rsa`, `secret`, `credentials`, `.pem`, …).
2. **Command safety** — blocklist dangerous patterns (`rm -rf /`, `curl … | sh`,
   fork bombs); allowlist rules (`Bash(git *)`, `Edit(src/**)`); destructive/network
   commands still confirm at the gate.
3. **Path jail** — reuse `AccessPolicy`, scoped to the session workspace root; edits
   outside prompt for grant.
4. **Prompt-injection stance** — file/web/tool content is *untrusted data*; actions are
   governed by the gate regardless of what the model "decides"; a system reminder warns
   about injected instructions.
5. **Egress disclosure** — per-session disclosure of which endpoint receives data; keys
   stored via `keyring`.

Every decision continues to be written to `AuditLog`.

### 4.4 Autonomy & UX

- **Modes:** `plan` (read-only tool preset → structured, editable plan card) and `act`.
  Plan mode is a restricted preset, not a state-machine mode (per OpenDev — avoids
  state-stuck bugs).
- **Approve → act:** approve the whole plan once; in-workspace edits auto-apply;
  shell/out-of-scope/destructive re-prompt at the gate.
- **Checkpoints:** each turn snapshots via a shadow git ref → one-command rewind
  (`jack undo`) and a checkpoint timeline in the drawer.
- **Diffs:** streamed diff cards with per-hunk accept/reject in the drawer; colorized
  `y/n/e` in the CLI.
- **Streaming:** live token stream + tool-step trace with a cost/token meter (extends
  `EventBus` — add token frames alongside the existing `publish_step`).
- **Creative touches:** `@file` / `@symbol` mentions to pin context; `/plan /test
  /commit /review` slash commands; "explain this change" on a hunk; and the
  differentiator — **coding by voice on macOS**.

### 4.5 Surfaces & protocol

- **`jack` CLI (cross-platform, Python thin client).** Connects to the warm daemon over
  the local socket; auto-spawns the daemon on first use. Commands: `jack "…"`,
  `jack code`, `jack /plan`, `jack review`, `jack undo`, `jack sessions`. No macOS-only
  imports on this path.
- **Chat drawer (macOS).** Reuses `EventBus` → WebSocket; new coding UI (plan cards,
  step trace + cost meter, diff cards, checkpoint timeline).
- **Daemon protocol.** v1 extends REST `/chat` + WebSocket with: sessions,
  plan-approve, diffs, checkpoints, streaming tokens. **Phase 4** adds an **ACP
  adapter** (JSON-RPC over stdio) so Zed/JetBrains/Kiro drive the same harness — the
  concrete "port to any IDE without rebuilding the core" mechanism.

### 4.6 Differentiators & UX principles (research-backed)

What makes Jack better *to use* than existing coding CLIs, and the principles the
build must uphold. Each ties to a documented pain point in today's tools.

**The seven differentiators:**

1. **One resumable session, every surface.** The #1 complaint about terminal agents is
   context loss — "each session starts fresh." Our sessions persist (transcript +
   summary + checkpoints) and are **resumable and portable**: start a task in the CLI,
   continue it in the macOS chat drawer (or later an IDE) — *same session id, same
   context*. No re-establishing context across days or surfaces.
2. **Coding by voice (macOS).** No other coding agent lets you dictate a change
   hands-free and watch the plan card appear. Unique to Jack's heritage.
3. **Any LLM via your key — no lock-in, no surprise pricing.** Directly answers the
   Cursor credit-pricing / cost-overrun frustration. Bring OpenAI-compatible,
   Anthropic, Gemini, OpenRouter, or a local endpoint; swap with one flag.
4. **Warm daemon = instant, stateful CLI.** No cold start (vs. Aider ~2.5s); context
   lives in the daemon across invocations, so `jack "…"` is immediate and remembers.
5. **Temporal change graph, not transcript soup.** "A long transcript is a tax on
   memory." Our primary artifact is the **checkpoint timeline** — a scrubbable graph of
   what changed each turn, with one-command rewind — not 1,500 lines of logs.
6. **Trust-persistence-safe by design.** Countering "approve once, exploit forever":
   session grants are **narrow, explicit, time/scope-bounded, revocable, and audited**
   — a grant shows exactly what it permits (`Bash(git *)`, not "all commands"), is
   listed in `jack grants`, and expires with the session.
7. **Privacy-first, transparent egress.** Secret redaction before any send, per-turn
   disclosure of *which endpoint receives which bytes*, and a **"0 bytes left your
   machine" badge** when a local model is selected. Trust as a visible feature.

**UX principles (apply to every surface):**

- **Summary before detail; never dump.** Surface the plan/outcome first; logs and full
  diffs are progressive-disclosure, one keystroke away. Counters diff-overload and
  "default to apply-all."
- **Steer before act.** The plan card is editable — reorder, strike, or amend steps
  before approving. Progressive autonomy dial (`plan → confirm → auto`) the user raises
  as trust builds; supervised by default.
- **Always an escape hatch.** Every turn is undoable (checkpoints); Ctrl-C is graceful
  (saves partial work); the model can be interrupted and redirected mid-stream.
- **Ambient cost/context meter + budget guard.** Live tokens/$/ETA always visible; a
  `max_spend`/`max_time` guard pauses and asks before blowing a budget.
- **Explain on demand.** "Why this change?" on any hunk; the agent's reasoning is
  available but not forced into the stream.
- **Honest failure & doom-loop candor.** On repeated failure the agent *says so*
  ("I've tried this twice; here's what I know, here are options") instead of silently
  looping — surfacing the doom-loop guard as a trust signal.
- **Keyboard-first.** Single-key approvals (`a`/`e`/`q`, `y`/`s`/`n`), fast and
  muscle-memory-friendly.
- **Right surface for the task.** CLI for flow/automation, drawer for visual/diff work,
  voice for hands-free — one engine underneath, so switching costs nothing.

## 5. Data flow (one coding turn)

```
user text (CLI or drawer)
  → daemon → Orchestrator.run_text_turn(session, text)
    → AgentHarness.run_turn(session, text, execute):
        assemble(system_prompt[coder] + repo map + history + summary)
        loop:
          model.send(messages, coder tools) → text | tool_calls
          for each call: execute(call)
             → PermissionGate: secret-redact → risk classify → confirm if needed
               → registry.dispatch → AuditLog
          append results; compact if needed; doom-loop check
        return reply
  → EventBus streams: state, tokens, steps, diffs, plan cards, cost meter
  → each turn: git checkpoint snapshot
```

## 6. Error handling

- Tools never raise out of `dispatch`; failures become `ToolResult(ok=False)` (existing
  contract) so a bad tool can't crash the loop.
- Provider/network errors: adapter surfaces a typed error; harness retries with backoff
  once, then returns a graceful failure turn.
- Doom-loop / round cap: harness forces a tools-off final answer (existing behavior).
- Checkpoints make destructive mistakes recoverable (`jack undo`).
- Cross-OS: shell/path failures degrade with actionable messages (never a raw
  traceback to the user).

## 7. Testing strategy

Matches existing conventions (explicit fakes, no mocking framework; mypy strict; ruff):

- Harness loop with a `FakeChatModel` (tool-call scripting, doom-loop, compaction).
- Each code tool unit-tested: fuzzy-match edit success/failure, path-jail escapes,
  command blocklist/allowlist, output truncation.
- Repo-map pure ranking (no tree-sitter runtime in unit tests where possible; gate
  heavy paths behind an integration marker).
- Secret-redaction pass.
- Provider adapters against recorded/fake responses (no live network in unit tests).
- CI matrix gains Linux (and ideally Windows) for the coding path.

## 8. Logging

New component tags per CLAUDE.md ("add logs as you build"): `[harness]`, `[coder]`,
`[provider]`. Seam events (INFO): plan proposed, plan approved, checkpoint saved, gate
decision, provider selected. No per-token/per-frame logs.

## 9. Config additions (`config.py`, the single source)

- `providers: list[Provider]` — each `(id, base_url, model, adapter)`.
- `default_provider: str`.
- `coding_autonomy: str` — `plan` (default) | `confirm` | `auto`.
- `checkpoints: bool` (default true).
- `command_allowlist: list[str]`, `command_blocklist: list[str]`.
- Keys move from the macOS-only Keychain call to the cross-platform `keyring` library
  (Keychain on macOS, Credential Locker on Windows, Secret Service on Linux).

## 10. Roadmap (epic phases)

- **Phase 1 — Harness & providers (v1):** extract `AgentHarness`; `ChatModel` + adapters;
  `Session` + transcripts; any-LLM-via-key (providers + keyring).
- **Phase 2 — Code tools & security gate (v1):** edit/grep/glob/run_command/git; repo
  map; plan mode + checkpoints; secret redaction + command safety; `coder` profile +
  basic `jack` CLI.
- **Phase 3 — Surfaces & UX:** CLI polish (optional TUI); chat-drawer plan/diff/checkpoint
  UX; voice coding.
- **Phase 4 — Port anywhere:** ACP bridge; subagents; LSP + code-MCP; Zed/JetBrains.

The first implementation plan covers Phases 1–2.

## 11. Risks & open questions

- **Refactor blast radius:** extracting the loop touches `Orchestrator` and both LLM
  classes. Mitigation: keep the `ChatModel` seam minimal; land the harness with the
  assistant profile first (behavior-preserving), then add the `coder` profile.
- **Local-model coding quality:** local models are weak at multi-file coding; the
  provider-agnostic layer lets users bring a stronger model. Set expectations in docs.
- **Cross-OS shell + git:** Windows shell/path edge cases. Mitigation: shell abstraction
  + CI matrix; treat Windows as best-effort for v1 if needed.
- **Secret redaction completeness:** redaction is defense-in-depth, not a guarantee;
  pair with clear egress disclosure and the path-jail.
- **Repo-map cost on huge repos:** cap/paginate; cache; self-heal.

## References

- OpenDev terminal-agent harness lessons (arXiv 2603.05344): one parameterized agent,
  five-stage compaction, decision-point reminders, five-layer defense-in-depth, plan
  mode via restricted preset, doom-loop detection.
- Aider: tree-sitter repo map, search/replace edit formats, architect/editor split.
- Agent Client Protocol (ACP): JSON-RPC-over-stdio editor↔agent standard (Phase 4).
- Reference implementation: `/Users/mohamedjakkariyar/work/claude-code` (Tool
  self-containment, permission-as-middleware, session-as-JSONL, bridge for IDE).
