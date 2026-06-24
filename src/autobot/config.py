"""Typed application settings, persisted as a single JSON file.

One source of truth: ``~/.autobot/settings.json``. Precedence is simply
**settings.json > field defaults** — a missing file or missing key falls back to
the dataclass default. There are no environment variables; the Settings view
(via the daemon) writes the file, and :meth:`Settings.load` reads it at startup.

Secrets (API keys) are **not** stored here — they live in the macOS Keychain
(:mod:`autobot.secrets`), so this file never contains credentials.

English-only is a fixed product constraint (see the roadmap): the STT model is an
English-only build and the LLM is instructed to answer in English.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = "~/.autobot/settings.json"

# --- Defaults -------------------------------------------------------------
# qwen3:8b is the most reliable small tool-caller (it actually emits tool calls
# instead of narrating them). Drop to "qwen2.5:3b"/":1.5b" for snappier replies
# at the cost of tool reliability.
_DEFAULT_LLM_MODEL = "qwen3:8b"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
_DEFAULT_LLM_MAX_TOKENS = 120
# A fast, strong tool-calling Claude (NOT a coding model). Model names change, so
# this is just a sensible default the user can change in the Settings view.
_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
_DEFAULT_STT_ENGINE = "faster_whisper"
_DEFAULT_STT_MODEL = "small.en"
_DEFAULT_STT_DEVICE = "cpu"
_DEFAULT_STT_COMPUTE_TYPE = "int8"
_DEFAULT_STT_BEAM = 5

_DEFAULT_SANDBOX_DIR = "~/.autobot/workspace"
_DEFAULT_AUDIT_DB = "~/.autobot/audit.db"

_DEFAULT_INPUT_MODE = "wake"
_DEFAULT_WAKE_DETECTOR = "stt"
_DEFAULT_WAKE_PHRASE = "jack"
_DEFAULT_WAKE_MODEL = "hey_jarvis"

_DEFAULT_TTS_VOICE = "~/.autobot/voices/en_US-ryan-high.onnx"
# Absolute (under ~/.autobot) so it works regardless of the working directory —
# e.g. launched from the bundled .app, whose CWD is "/".
_DEFAULT_SESSION_DIR = "~/.autobot/sessions"
_DEFAULT_LOG_DIR = "~/.autobot/logs"
_DEFAULT_LOG_LEVEL = "DEBUG"
_DEFAULT_LOG_CONSOLE_LEVEL = "WARNING"

# Biases the speech recognizer toward the command vocabulary (app names, the
# assistant's name) so short proper nouns aren't misheard — e.g. "Chrome" → "home",
# "Firefox" → "fire fox". Whisper's standard initial_prompt mechanism; phrased like
# real commands so it primes the decoder without forcing words in. Editable in
# settings.json to add your own apps.
_DEFAULT_STT_PROMPT = (
    "Commands for Jack: open or close apps like Safari, Chrome, Firefox, Microsoft Edge, "
    "Finder, Mail, Calendar, Notes, Terminal, Spotify, Slack, Visual Studio Code, Music, "
    "Messages, Photos, Preview, System Settings."
)

SAMPLE_RATE = 16_000
"""Sample rate (Hz) the whole pipeline assumes. Whisper expects 16 kHz mono."""

CHANNELS = 1
"""Mono capture."""


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime-tunable configuration for the assistant."""

    # --- language model ---
    # Which brain: "ollama" (local, default) or "anthropic" (cloud, opt-in). The
    # Anthropic API key lives in the Keychain, never in this file.
    llm_provider: str = "ollama"
    llm_model: str = _DEFAULT_LLM_MODEL
    ollama_host: str = _DEFAULT_OLLAMA_HOST
    anthropic_model: str = _DEFAULT_ANTHROPIC_MODEL
    anthropic_max_tokens: int = 512
    # Cloud context window (prompt-token budget). 0 -> resolve from a per-model
    # default and self-correct from the API's "… > N maximum" error, so a 200k,
    # 1M, or smaller model is all handled without hand-tuning. Local (Ollama) is
    # auto-detected separately via context_tokens.
    anthropic_context_tokens: int = 0
    llm_temperature: float = 0.0
    llm_max_tokens: int = _DEFAULT_LLM_MAX_TOKENS
    # qwen3 reasoning. ON makes tool-calling far more reliable (the model decides
    # "they want me to act -> call the tool" instead of answering a 'can you…?' as
    # yes), at some latency. Reasoning goes to the model's `thinking` field.
    llm_think: bool = True
    # Conversational memory / dynamic context management. context_tokens=0 -> auto
    # detect the model's window via Ollama; compact older turns at compact_at.
    context_tokens: int = 0
    compact_at: float = 0.85
    keep_recent_messages: int = 6
    # --- speech-to-text ---
    # Engine: "faster_whisper" (CPU/int8, default) or "whisper_cpp" (GPU via Metal
    # on Apple Silicon — runs bigger models faster; needs the 'whispercpp' extra).
    stt_engine: str = _DEFAULT_STT_ENGINE
    stt_model: str = _DEFAULT_STT_MODEL
    stt_device: str = _DEFAULT_STT_DEVICE
    stt_compute_type: str = _DEFAULT_STT_COMPUTE_TYPE
    stt_beam_size: int = _DEFAULT_STT_BEAM
    # Vocabulary hint passed to the recognizer each transcription (see above).
    stt_prompt: str = _DEFAULT_STT_PROMPT
    # --- tools / sandbox ---
    sandbox_dir: str = _DEFAULT_SANDBOX_DIR
    audit_db: str = _DEFAULT_AUDIT_DB
    # Seconds to wait for a spoken yes/no before auto-cancelling a destructive
    # action (silence/timeout cancels — nothing destructive runs without a clear yes).
    confirm_timeout_s: float = 30.0
    # --- listening (Phase 2) ---
    input_mode: str = _DEFAULT_INPUT_MODE
    wake_detector: str = _DEFAULT_WAKE_DETECTOR
    wake_phrase: str = _DEFAULT_WAKE_PHRASE
    wake_model: str = _DEFAULT_WAKE_MODEL
    wake_threshold: float = 0.3
    vad_threshold: float = 0.5
    # How long a pause must last before we treat speech as finished. Generous on
    # purpose: a shorter value cuts people off when they pause mid-thought. Tunable
    # in the Settings view (applies live).
    end_silence_ms: int = 1400
    save_audio: bool = False
    # Hard cap on a single utterance, so capture can't run forever. Generous so a
    # long, deliberate request isn't truncated mid-sentence.
    max_utterance_s: float = 60.0
    wake_preroll_ms: int = 400
    follow_up_window_s: float = 30.0
    # If a captured phrase looks cut off mid-thought (no terminal punctuation, ends
    # on a connective word), briefly re-open the mic and append rather than answer a
    # half-sentence. A safety net on top of end_silence_ms.
    reopen_on_incomplete: bool = True
    # How you interact: "chat" (typed text in the side drawer — mic off, replies
    # shown not spoken; the DEFAULT so a fresh install needs no mic and no voice
    # model) or "voice" (hands-free wake word + speech, enabled once the voice models
    # are downloaded). Switchable live; the voice loop idles while in chat mode.
    interaction_mode: str = "chat"
    # --- voice output (Phase 3) ---
    tts_enabled: bool = True
    tts_voice: str = _DEFAULT_TTS_VOICE
    speak_acknowledgements: bool = True
    # Brief pause after Jack finishes speaking before the mic re-opens (half-duplex
    # fallback path), so the tail of its own voice can't bleed into the next capture.
    tts_settle_ms: int = 250
    # Barge-in: let the user talk over Jack mid-reply. On by default but it only
    # actually engages when the AEC full-duplex path is active — i.e. the mic runs
    # through macOS Voice-Processing AND Jack's TTS is rendered through that same
    # engine, so his voice is cancelled from the mic. When AEC isn't available we fall
    # back to half-duplex (don't listen while speaking) automatically. Needs `aec`.
    barge_in: bool = True
    # How long the user must keep speaking before it counts as a barge-in. A real
    # interruption is sustained; a brief echo/transient is a flicker. Set high enough
    # that residual echo never trips it, low enough that interrupting still feels
    # instant. ~250 ms is a good balance.
    barge_in_min_speech_ms: int = 250
    # Echo cancellation via macOS Voice-Processing I/O (mic capture + TTS playback
    # through one engine, so Jack's voice is cancelled from the mic). On by default;
    # if it can't start (no pyobjc, no mic permission, VPIO error) we fall back to the
    # plain mic and half-duplex automatically — nothing breaks.
    aec: bool = True
    # --- web search (opt-in, off-device) ---
    allow_web: bool = False
    web_results: int = 5
    # "auto" uses the API when a key is in the Keychain, else ddgs scraping;
    # "searchspace" forces the API; "ddgs" forces scraping. The key is in Keychain.
    web_provider: str = "auto"
    web_api_url: str = "https://q.searchspace.io/v1/search"
    web_backend: str = "duckduckgo,bing,brave,google"
    # --- daemon (Phase 3c) ---
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 8765
    # --- capabilities ---
    allow_app_control: bool = True
    allow_system_info: bool = True
    allow_memory: bool = True
    memory_db: str = "~/.autobot/memory.db"
    # --- debugging / logging ---
    session_log: bool = True
    session_dir: str = _DEFAULT_SESSION_DIR
    # Keep only the most recent N session transcripts; older ones are pruned on
    # startup so the sessions folder never accumulates hundreds of files.
    session_keep: int = 20
    show_debug: bool = True
    log_dir: str = _DEFAULT_LOG_DIR
    log_level: str = _DEFAULT_LOG_LEVEL
    log_console_level: str = _DEFAULT_LOG_CONSOLE_LEVEL
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS

    @classmethod
    def load(cls, path: str | Path = DEFAULT_SETTINGS_PATH) -> Settings:
        """Build settings from ``settings.json``, overlaying it on the defaults.

        Unknown keys are ignored and badly-typed values fall back to the default,
        so a hand-edited or partial file can never crash startup.
        """
        data = _read_settings(path)
        defaults = cls()
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in data:
                coerced = _coerce(data[f.name], getattr(defaults, f.name))
                if coerced is not None:
                    overrides[f.name] = coerced
        if "wake_phrase" in overrides:  # matched against lower-cased transcripts
            overrides["wake_phrase"] = str(overrides["wake_phrase"]).lower()
        return replace(defaults, **overrides)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the Settings view / persistence (no secrets are stored)."""
        return asdict(self)


def read_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> dict[str, Any]:
    """Public read of the raw settings file (sparse — only keys the user set)."""
    return _read_settings(path)


def setting_names() -> set[str]:
    """The set of valid setting keys (the dataclass field names)."""
    return {f.name for f in fields(Settings)}


def _read_settings(path: str | Path) -> dict[str, Any]:
    """Read the settings JSON, returning ``{}`` if missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(data: dict[str, Any], path: str | Path = DEFAULT_SETTINGS_PATH) -> None:
    """Persist ``data`` to the settings file (0600), creating parents as needed."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):  # best effort on exotic filesystems
        p.chmod(0o600)


def _coerce(value: Any, default: Any) -> Any:
    """Coerce a JSON value to the default's type; ``None`` means 'use the default'."""
    expected = type(default)
    try:
        if expected is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if expected is int:
            return int(value)
        if expected is float:
            return float(value)
        if expected is str:
            return str(value)
        return value
    except (ValueError, TypeError):
        return None
