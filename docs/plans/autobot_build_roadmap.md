# Local Voice Assistant — Build Roadmap

A privacy-first, zero-cost, on-device personal assistant. Reference to follow during the build.

**Core principle:** Build a *walking skeleton* (a dumb end-to-end loop that runs), then deepen it. Do **not** build layer by layer and integrate at the end — the failures in this kind of system live in the *seams* between components (audio formats, async handoffs, state-machine deadlocks, unparseable tool-call JSON). Order the work by **risk**, not by the diagram's top-to-bottom layout. Tackle scary unknowns first; leave easy plumbing for last.

---

## Golden rules (apply at every phase)

- **Always have something that runs.** Every phase ends with a working loop, just a more capable one.
- **Define each boundary as a clean interface up front** (see Interface Contracts). This is what lets you stub one component while building another, and swap models per hardware tier as a *config change*, not a rewrite.
- **Engine is headless.** The core runs as a background daemon exposing a localhost API. Every UI (terminal, desktop) is a thin client of that daemon — never build the assistant *as* a UI app.
- **Everything stays on-device.** No audio, text, or memory leaves the machine. This is the whole point.
- **The permission gate is not optional polish.** An LLM that can run shell commands is a loaded gun. Design the gate before the tools it guards.
- **English only, both directions.** STT (voice→text) and TTS (text→voice) handle **English only** — this is a fixed product constraint, not a phase-1 simplification. Pick English-optimized models (Moonshine, Parakeet) over multilingual ones; you get better accuracy, lower hallucination, and a smaller footprint by dropping languages you'll never use. Keep STT/TTS behind their interfaces so this stays a model choice, but do not carry multilingual options forward.

---

## Phase 0 — Thin spine (push-to-talk) ✅ DONE (2026-06-19)

**Goal:** Validate the single hardest hidden assumption — that your chosen local LLM actually emits usable tool calls.

- [x] Press Enter to record a clip (no wake word, no VAD yet)
- [x] Clip → faster-whisper `base.en` (English-only weights) → text
- [x] Text → Ollama, with exactly one trivial tool registered (`get_time`)
- [x] Parse the tool call, run it, print the reply
- [x] **Done when:** you speak "what time is it", the LLM calls `get_time`, and the right answer prints. ✅ Verified 2026-06-19 — `qwen3:8b` on M2/16GB; "What is the time?" → `get_time` → correct reply.

> If the LLM won't produce clean tool-call JSON, find out now (hour one), not at week six. Test 2–3 models here before committing.

---

## Phase 1 — Real orchestrator + one guarded tool ✅ DONE (2026-06-19)

**Goal:** Exercise the most dangerous part of the system early — a tool that genuinely *acts*.

- [x] Build the orchestrator as a proper **state machine** (idle → listening → transcribing → planning → executing → responding, plus an "ask user to clarify" branch). This is the backbone everything else plugs into. — `orchestrator/state_machine.py` (`StateMachine` + `Orchestrator`)
- [x] Add one genuinely-acting tool (e.g. create/move a file) — `tools/filesystem.py`: `create_file`/`move_file` (WRITE), `delete_file` (DESTRUCTIVE)
- [x] Put it behind the **permission gate**: classify risk → confirm destructive actions → sandbox → write to an audit log (SQLite) — `tools/permission.py` + `tools/sandbox.py` (path-jail) + `tools/audit.py`
- [x] **Done when:** a destructive action prompts for confirmation, executes only on yes, and leaves an audit-log entry. ✅ Verified 2026-06-19 (40 unit tests + integration smoke: WRITE runs unprompted, DESTRUCTIVE blocked unless confirmed, sandbox escape refused, all four audited).

> Confirmation policy: destructive-only (READ_ONLY + WRITE run directly but are still audited). Sandbox: path-jail to `~/.autobot/workspace` (`AUTOBOT_SANDBOX_DIR`). Audit DB: `~/.autobot/audit.db` (`AUTOBOT_AUDIT_DB`). The gate sits between *planning* and *executing* — the LLM never touches side effects directly; it calls an injected executor wired to the gate.

---

## Phase 2 — Always-on listening layer

**Goal:** Replace push-to-talk with hands-free wake. This is the real-time audio / threading risk — prove it in isolation now that the rest works.

- [ ] Mic capture into a ring buffer (sounddevice / PortAudio)
- [ ] Wake-word detection (openWakeWord, ONNX) — train your custom "Agent name" phrase
- [ ] VAD (silero-vad) to detect end-of-speech and cut the clip
- [ ] Wire: wake word fires → capture until VAD endpoint → hand clip to the Phase 0/1 pipeline
- [ ] **Done when:** saying the wake word, then a command, runs the full loop with no keypress.

> Gate STT strictly on VAD-detected speech. This also neutralizes Whisper's silence-hallucination problem.

---

## Phase 3 — Voice output + terminal UI

- [ ] TTS: Piper (CPU/fast tier), Kokoro (GPU quality tier) — behind the TTS interface
- [ ] Terminal client (Textual) connecting to the daemon API; add the animation here
- [ ] **Done when:** the assistant speaks its replies and the terminal shows live state.

---

## Phase 4 — Memory / self-learning

- [ ] Structured user profile (SQLite): preferences, names, recurring tasks
- [ ] Episodic memory: embed past interactions (sqlite-vec) for semantic recall
- [ ] Orchestrator queries memory before reasoning (RAG); writes back after each turn
- [ ] **Done when:** the assistant recalls a fact from a previous session.

---

## Phase 5 — Hardware tiering, hardening, packaging

- [ ] Hardware profiler: detect RAM/VRAM at install, pick model tier (STT + LLM + TTS) automatically
- [ ] Tier presets (see table) selectable via config
- [ ] Error handling, graceful degradation, logging
- [ ] Package the daemon + clients for distribution
- [ ] (Optional) Add the desktop client (Tauri) — just another thin client of the same daemon
- [ ] **Done when:** a fresh machine runs the right models for its specs with no manual tuning.

---

## Interface Contracts (define before Phase 0)

Keep each component behind a stable interface so models are swappable per hardware tier.

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
| Terminal UI | Textual | Async TUI capable of animation |
| Desktop UI (later) | Tauri (Rust + web) | Tiny binary vs Electron; talks to same daemon |

**On Rust:** the only hot path where Python's GIL/latency realistically bites is the always-on audio + wake-word loop. openWakeWord runs many models in real time even on a Pi 3, so don't pre-optimize. If you later *measure* audio jitter, move only that capture loop to Rust; leave orchestration in Python. A full Rust rewrite costs weeks of velocity for near-zero runtime gain, since inference is already native.

---

## Hardware Tiers (for Phase 5 profiler)

| Tier | Hardware | STT | LLM | TTS |
|------|----------|-----|-----|-----|
| Low | CPU-only, ≤8GB RAM, Pi | Moonshine tiny/base | Phi-4 / small Qwen (Q4) | Piper (EN) |
| Mid | 16GB RAM, Apple Silicon, modest GPU | Moonshine base, or faster-whisper `small.en`/`medium.en` | Gemma 4 9B / Qwen ~8B | Piper (EN) |
| High | NVIDIA GPU 12GB+ | Parakeet V3 (via NeMo) or faster-whisper `large-v3` (EN) | Qwen 3.x 32B / Gemma 4 26B | Kokoro (EN) |

> STT note: this assistant is **English-only**, and you transcribe short command clips bounded by VAD, not a live stream — so optimize for accuracy on short English utterances, hallucination robustness, and footprint, not language coverage. That makes the English-first models the right default rather than a compromise: **Moonshine** (efficient, low-hallucination, English MIT-licensed, ~245M base) for the low/mid tiers, **Parakeet V3** (strong English accuracy, Apache 2.0, NeMo setup overhead) on capable NVIDIA GPUs. faster-whisper `*.en` is the fallback; avoid multilingual Whisper builds entirely — they carry the silence-hallucination risk and weight you don't need.

---

## Reference projects to study first

- **Home Assistant voice pipeline + Wyoming protocol** — an existing open standard for chaining wake-word / STT / TTS as interchangeable services. Maps almost exactly onto Phases 2, 0/STT, and 3. Even if you don't adopt it, its interface boundaries are a proven reference for your own.
- **Model Context Protocol (MCP)** — the tool/action interface standard for the permission-gated action layer.

---

## Risk-ordering cheat sheet

Build in this order *because of risk*, regardless of where a component sits in the architecture diagram:

1. Reliable local tool-calling (Phase 0)
2. The permission gate + a real acting tool (Phase 1)
3. Real-time audio handoff: wake word + VAD + ring buffer (Phase 2)
4. Output + UI (Phase 3)
5. Memory (Phase 4)
6. Tiering, hardening, packaging (Phase 5)
