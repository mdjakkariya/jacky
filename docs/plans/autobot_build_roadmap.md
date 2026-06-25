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

> Confirmation policy: destructive-only (READ_ONLY + WRITE run directly but are still audited). Sandbox: path-jail to `~/.autobot/workspace`; audit DB: `~/.autobot/audit.db`. All config lives in `~/.autobot/settings.json` (defaults in `config.py`) — no env vars. The gate sits between *planning* and *executing* — the LLM never touches side effects directly; it calls an injected executor wired to the gate.

---

## Phase 2 — Always-on listening layer ✅ DONE (2026-06-19)

**Goal:** Replace push-to-talk with hands-free wake. This is the real-time audio / threading risk — prove it in isolation now that the rest works.

- [x] Mic capture into a ring buffer (sounddevice / PortAudio) — `io/listening.py` `MicFrameSource` (persistent stream → queue, 512-sample frames) + `FramePrebuffer` pre-roll
- [x] Wake-word detection (openWakeWord, ONNX) — `io/wake_vad.py` `OpenWakeWord`; defaults to the pretrained `hey_jarvis` model (a custom "Jack" phrase needs offline training — see README)
- [x] VAD (silero-vad) to detect end-of-speech and cut the clip — `io/wake_vad.py` `SileroVad` + the pure `TrailingSilenceEndpointer`
- [x] Wire: wake word fires → capture until VAD endpoint → hand clip to the Phase 0/1 pipeline — `io/listening.py` `WakeWordVadRecorder` (same `AudioSource` contract, so orchestrator/STT/gate unchanged)
- [x] **Done when:** saying the wake word, then a command, runs the full loop with no keypress. ✅ Logic verified 2026-06-19 (49 unit tests incl. wake-gating, pre-roll, VAD endpointing, max-utterance cap, with fakes). Live mic/model run is user-side.

> Gate STT strictly on VAD-detected speech. This also neutralizes Whisper's silence-hallucination problem.

> **Chat** is the default mode (`interaction_mode: chat`); voice is opt-in. Within voice, hands-free (`input_mode: wake`) is the default and push-to-talk is `input_mode: ptt`. Config is `settings.json`, not env vars. The wake/VAD models are an optional install (`uv sync --extra wake`) so the core stays light. Real-time logic (endpointing, pre-roll) is split from the mic/models and unit-tested; the model wrappers and mic are injected, so the loop is testable without hardware.

---

## Phase 3 — Voice output + UI ✅ DONE

- [x] **TTS** — `TextToSpeech` protocol + `tts/piper_tts.py` (Piper) + `NullTTS` fallback; orchestrator speaks replies. Config: `tts_enabled` / `tts_voice` in settings.json; optional `tts` extra.
- [x] **Headless daemon** — `daemon/server.py` (FastAPI + localhost-only WebSocket `/ws`, `/healthz`), `core/events.py` (`OrbState` + thread-safe `EventBus`), `daemon/runner.py`. Streams `{state, amplitude}`: mic RMS while listening, TTS RMS while talking.
- [x] **Floating orb + chat drawer ("Jack")** — a **Tauri** shell (Rust + system webview) is the product surface, not the originally-planned Textual TUI (which was dropped). The orb (`ui/orb/index.html`, WebGL) floats over apps/Spaces, never steals focus, reacts to state; a right-docked **chat drawer** (`ui/orb/chat.html`) is the default typed UI. Global shortcuts (⌘⌃J summon, ⌘⌃C chat, ⌘⌃V voice, …), tray menu, Settings view.
- [x] **Done when:** speaking drives the orb idle→listening→thinking→talking with live reactive motion, it survives app/Space switches, and chat works without voice. ✅ Shipped (v0.4.0).

> Visual reference: `docs/ui/jack_orb_prototype.html`. Full plan: `docs/plans/autobot_floating_orb_ui_plan.md`.

---

## Phase 4 — Memory / self-learning

- [x] Structured user profile (SQLite): name + learned facts — **done (fd54851):**
  `memory/store.py` (on-device `~/.autobot/memory.db`, single evolving profile,
  deduped facts, `forget`), `tools/memory.py` (`set_name`/`remember`/`forget`,
  gated WRITE). Model auto-saves durable facts inline during a turn (reasoning on).
- [x] Orchestrator/LLM queries memory before reasoning, writes back after — profile
  injected into the prompt each turn; first-meeting greeting introduces Jack and
  asks the name when unknown; warm-friend persona.
- [ ] Episodic memory: embed past interactions (sqlite-vec) for semantic recall —
  deferred until the flat profile outgrows direct injection (RAG-ready).
- [x] **Done when:** the assistant recalls a fact from a previous session. ✅
  (name + facts persist across restarts; behind `allow_memory` in settings.json).

---

## Shipped beyond the original phases

Decisions made during the build that aren't captured above:

- **Optional cloud LLM (Anthropic)** behind the same `LanguageModel` interface — prompt caching, dynamic per-model context window, summarization compaction, a dev context meter. Local Ollama stays the default.
- **Chat-first product** — chat is the default mode; the orb stays hidden until voice is enabled. Typed turns, "New chat", welcome screen, rotating input hints.
- **On-demand voice + small build** — the Piper voice is no longer bundled (~115MB off the `.dmg`); voice/STT/wake download on demand via `voice_setup.py` (`/voice/status`, `/voice/download`) with progress in Settings. STT/mic/TTS build lazily on first voice use.
- **Release automation** — git-cliff changelog + GitHub release notes; `make bundle` / `make publish-orb`; in-app update-available banner (GitHub Releases). `multiprocessing.freeze_support()` guards the frozen entry.
- **File tools + generic action cards** — on-device Spotlight search (`tools/files.py`: `search_files`, `open_path`, `reveal_path`) with stop-word/fuzzy matching; a reusable engine→chat **action-card** channel (`ChoicesEvent` / `publish_choices` / `POST /action` → `Orchestrator.run_tool`) so any tool can offer clickable choices that run through the gate with no LLM turn.
- **Dev debug report button** — a chat-header button (dev builds) copies a concise, per-session, redacted report (`/report/concise`, scoped via `mark_session`), distinct from the full GitHub-issue report.
- **Public website + docs** — a static site under `docs/` (landing + getting-started), served by GitHub Pages from `/docs` (`.nojekyll`); matches the app theme + animated orb/chat showcase. Lean README with live badges + star-history; cookieless GoatCounter analytics; Codecov coverage in CI.

---

## Next directions

Current plan, ordered by value-for-effort. Every new tool reuses the registry +
permission gate + action-card pattern, so each is a small, consistent addition.

**Track 1 — Capability growth (active).** More of what Jack can *do*:
- [x] On-device file search + open / reveal in Finder, surfaced as clickable cards.
- [x] Clipboard read/write — "copy that", "what's on my clipboard" (`tools/clipboard.py`).
- [ ] Reminders & Calendar (EventKit via `osascript`) — "remind me at 5", "what's
  on my calendar" (WRITE/automation, gated).
- [ ] Screenshot — capture screen/region to a file (`screencapture`); pairs with the
  action card to open/reveal it.
- [ ] System toggles — volume, brightness, dark mode, Do Not Disturb, sleep (WRITE).
- [ ] Notes — create/append a note.
- [ ] Multi-step plans — chain tools in one turn (engine already supports it).

**Track 2 — Memory depth.** Episodic/semantic recall (sqlite-vec + a small on-device
embedding model) so Jack remembers past conversations, not just a flat profile (the
deferred Phase 4 item).

**Track 3 — Conversation & voice polish.** Streaming replies + markdown in chat
(stop/copy); a voice picker (multiple Piper voices); push-to-talk toggle; barge-in
tuning.

**Track 4 — Hardening & tiering.** Hardware profiler (auto STT/LLM/TTS tier by
RAM/GPU), graceful degradation, more UI/integration tests.

**Track 5 — Site/launch follow-ups (alongside).** Fill in the GoatCounter code, enable
Codecov, add screenshots / a short demo GIF to the site, set the repo's About URL.

**Distribution & trust — DEFERRED** (needs an Apple Developer ID, not available yet).
When resourced: code signing + notarization (removes the Gatekeeper warning, fixes the
Accessibility/Automation "Unknown", and unlocks real in-place auto-update via the Tauri
updater, replacing the notify-only banner).

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
