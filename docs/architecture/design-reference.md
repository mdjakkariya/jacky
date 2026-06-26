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

## Reference projects to study

- **Home Assistant voice pipeline + Wyoming protocol** — an existing open standard
  for chaining wake-word / STT / TTS as interchangeable services. Its interface
  boundaries are a proven reference for ours.
- **Model Context Protocol (MCP)** — the tool/action interface standard for the
  permission-gated action layer.
