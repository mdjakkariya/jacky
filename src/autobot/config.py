"""Typed application settings with environment-variable overrides.

A single immutable :class:`Settings` object is threaded through the app, so all
tunables live in one place and there are no scattered ``os.environ`` reads. Build
it once at startup with :meth:`Settings.from_env`.

English-only is a fixed product constraint (see the roadmap): the STT model is an
English-only build and the LLM is instructed to answer in English.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> None:
    """Load ``KEY=VALUE`` lines from a ``.env`` file into the environment.

    A tiny, dependency-free loader so secrets like ``AUTOBOT_WEB_API_KEY`` live in
    a gitignored ``.env`` instead of being exported every shell. Real environment
    variables always win (``setdefault``), comments/blank lines are skipped, an
    optional ``export`` prefix and surrounding quotes are stripped.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# --- Defaults -------------------------------------------------------------
# qwen3:8b is the most reliable small tool-caller (it actually emits tool calls
# instead of narrating them). We disable its "thinking" mode for speed. Drop to
# "qwen2.5:3b" or ":1.5b" for snappier replies at the cost of tool reliability.
_DEFAULT_LLM_MODEL = "qwen3:8b"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
# Cap reply length so spoken answers stay short and fast (tokens). Voice replies
# should be a sentence or two — a tight cap also curbs the model's rambling.
_DEFAULT_LLM_MAX_TOKENS = 120
# small.en is much more accurate than base.en on connected speech/names, still
# fast enough on M2 (~1-2s/clip). Drop to "base.en" for speed, "medium.en" for
# max accuracy. First run auto-downloads the weights.
_DEFAULT_STT_MODEL = "small.en"
# CTranslate2 has no Metal backend, so STT runs on CPU; int8 keeps it light.
_DEFAULT_STT_DEVICE = "cpu"
_DEFAULT_STT_COMPUTE_TYPE = "int8"
# Beam search width: 5 is notably more accurate than greedy (1) for a little
# more latency on short command clips.
_DEFAULT_STT_BEAM = 5

# Phase 1: where acting tools may operate, and where the audit trail is kept.
# Both stay inside a single private directory under the user's home.
_DEFAULT_SANDBOX_DIR = "~/.autobot/workspace"
_DEFAULT_AUDIT_DB = "~/.autobot/audit.db"

# Phase 2: always-on listening. "wake" = hands-free (wake word + VAD); "ptt" =
# push-to-talk (Enter).
_DEFAULT_INPUT_MODE = "wake"
# How the wake word is detected in hands-free mode:
#   "stt"         — transcribe each phrase and match the wake word in text.
#                   Handles continuous AND fast speech, and is reliable for a
#                   COMMON word the STT model knows well (e.g. "jack"). Default.
#   "openwakeword"— dedicated acoustic model; needs a pretrained/custom model for
#                   the phrase (e.g. "hey_jarvis"); no built-in "jack" model.
_DEFAULT_WAKE_DETECTOR = "stt"
# Spoken wake phrase matched in transcripts (STT detector). Pick a common,
# distinct word the STT model transcribes reliably. "jack" works well; rare
# names like "jarvis" get mis-transcribed by base.en. The last word is the
# trigger token, so "hey jack" also matches.
_DEFAULT_WAKE_PHRASE = "jack"
# openWakeWord pretrained model name (used only when wake_detector="openwakeword").
_DEFAULT_WAKE_MODEL = "hey_jarvis"

# Phase 3: voice output (TTS). Piper speaks replies on-device. Point tts_voice at
# a downloaded Piper voice (.onnx); if missing or disabled, we fall back to silent.
# Default is a male voice (Ryan) to match "Jack"; swap via AUTOBOT_TTS_VOICE.
_DEFAULT_TTS_VOICE = "~/.autobot/voices/en_US-ryan-high.onnx"

# Per-session transcript (readable conversation + debug notes) for debugging.
# Kept in the project folder by default so it's easy to open/share.
_DEFAULT_SESSION_DIR = "sessions"

# Logging: a rotating debug log you can share when reporting an issue. The file
# captures DEBUG; the console only shows WARNING+ so normal runs stay clean.
_DEFAULT_LOG_DIR = "~/.autobot/logs"
_DEFAULT_LOG_LEVEL = "DEBUG"
_DEFAULT_LOG_CONSOLE_LEVEL = "WARNING"

SAMPLE_RATE = 16_000
"""Sample rate (Hz) the whole pipeline assumes. Whisper expects 16 kHz mono."""

CHANNELS = 1
"""Mono capture."""


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime-tunable configuration for the assistant."""

    llm_model: str = _DEFAULT_LLM_MODEL
    ollama_host: str = _DEFAULT_OLLAMA_HOST
    llm_temperature: float = 0.0
    llm_max_tokens: int = _DEFAULT_LLM_MAX_TOKENS
    # Conversational memory with dynamic context management:
    #   context_tokens=0 -> auto-detect the model's window via Ollama (else this cap).
    #   When the prompt reaches compact_at of the window, older turns are summarized
    #   and only keep_recent_messages are kept verbatim. Scales with the model.
    context_tokens: int = 0
    # Compact at 85% (not 100%) and check BEFORE each turn, so a sudden large
    # message can't push a single prompt past the window. Margin absorbs estimate error.
    compact_at: float = 0.85
    keep_recent_messages: int = 6
    stt_model: str = _DEFAULT_STT_MODEL
    stt_device: str = _DEFAULT_STT_DEVICE
    stt_compute_type: str = _DEFAULT_STT_COMPUTE_TYPE
    stt_beam_size: int = _DEFAULT_STT_BEAM
    sandbox_dir: str = _DEFAULT_SANDBOX_DIR
    audit_db: str = _DEFAULT_AUDIT_DB
    # Phase 2: hands-free listening.
    input_mode: str = _DEFAULT_INPUT_MODE
    wake_detector: str = _DEFAULT_WAKE_DETECTOR
    wake_phrase: str = _DEFAULT_WAKE_PHRASE
    wake_model: str = _DEFAULT_WAKE_MODEL
    # Lower than openWakeWord's usual 0.5: measured peaks were ~0.8 for an
    # isolated "hey jarvis" but only ~0.35-0.40 when said continuously, so 0.30
    # catches both with margin (non-wake speech scores well under 0.1). Raise it
    # if you get false triggers; tune from the 'wake score=' debug log lines.
    wake_threshold: float = 0.3
    vad_threshold: float = 0.5
    # End the utterance after this much trailing silence. Wide enough that a brief
    # mid-sentence pause doesn't cut you off (which produces fragment mis-hears).
    end_silence_ms: int = 1000
    # Save each captured clip as a WAV next to the transcript, to inspect whether
    # a mis-hear is bad audio vs. the model. Off by default (uses disk).
    save_audio: bool = False
    max_utterance_s: float = 15.0
    # Audio kept from just *before* the wake word fires, prepended to the capture
    # so a command spoken in the same breath ("hey jarvis, what's the time") isn't
    # clipped by wake-word detection latency. 0 disables it.
    wake_preroll_ms: int = 400
    # After a turn, keep listening for a follow-up without the wake word for this
    # long (measured from when the reply finishes speaking); each turn resets it.
    # Generous so natural pauses + STT latency don't drop a follow-up. 0 disables.
    follow_up_window_s: float = 30.0
    # Phase 3: voice output.
    tts_enabled: bool = True
    tts_voice: str = _DEFAULT_TTS_VOICE
    # Speak a short "on it…" acknowledgement before running a (possibly slow)
    # tool, so the user isn't left in silence waiting.
    speak_acknowledgements: bool = True
    # Web search — the ONE feature that leaves the device. Off by default; the
    # tool is only registered when enabled, and every call is audited.
    allow_web: bool = False
    web_results: int = 5
    # Search provider: "auto" uses the API when a key is set, else falls back to
    # ddgs scraping; "searchspace" forces the API; "ddgs" forces scraping.
    web_provider: str = "auto"
    # API backend (e.g. SearchSpace). The key comes from AUTOBOT_WEB_API_KEY and is
    # NEVER stored in code/config. Endpoint is configurable to swap providers.
    web_api_url: str = "https://q.searchspace.io/v1/search"
    web_api_key: str = ""  # set via AUTOBOT_WEB_API_KEY only
    # Comma-delimited ddgs backends tried in order (the scraping fallback).
    web_backend: str = "duckduckgo,bing,brave,google"
    # Debugging aids.
    session_log: bool = True
    session_dir: str = _DEFAULT_SESSION_DIR
    show_debug: bool = True  # print per-turn token/compaction lines to the terminal
    # Logging.
    log_dir: str = _DEFAULT_LOG_DIR
    log_level: str = _DEFAULT_LOG_LEVEL
    log_console_level: str = _DEFAULT_LOG_CONSOLE_LEVEL
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``AUTOBOT_*`` / ``OLLAMA_HOST`` env vars.

        Defaults come from the dataclass fields above (the single source of truth):
        each env var falls back to ``d.<field>`` so the two can never drift apart.
        A ``.env`` file (if present) is loaded first. This is the only place the
        environment is read.
        """
        load_env_file(os.environ.get("AUTOBOT_ENV_FILE", ".env"))
        d = cls()
        return cls(
            llm_model=_env_str("AUTOBOT_LLM_MODEL", d.llm_model),
            ollama_host=_env_str("OLLAMA_HOST", d.ollama_host),
            llm_temperature=_env_float("AUTOBOT_LLM_TEMPERATURE", d.llm_temperature),
            llm_max_tokens=_env_int("AUTOBOT_LLM_MAX_TOKENS", d.llm_max_tokens),
            context_tokens=_env_int("AUTOBOT_CONTEXT_TOKENS", d.context_tokens),
            compact_at=_env_float("AUTOBOT_COMPACT_AT", d.compact_at),
            keep_recent_messages=_env_int("AUTOBOT_KEEP_RECENT", d.keep_recent_messages),
            stt_model=_env_str("AUTOBOT_STT_MODEL", d.stt_model),
            stt_device=_env_str("AUTOBOT_STT_DEVICE", d.stt_device),
            stt_compute_type=_env_str("AUTOBOT_STT_COMPUTE_TYPE", d.stt_compute_type),
            stt_beam_size=_env_int("AUTOBOT_STT_BEAM", d.stt_beam_size),
            sandbox_dir=_env_str("AUTOBOT_SANDBOX_DIR", d.sandbox_dir),
            audit_db=_env_str("AUTOBOT_AUDIT_DB", d.audit_db),
            input_mode=_env_str("AUTOBOT_INPUT", d.input_mode),
            wake_detector=_env_str("AUTOBOT_WAKE_DETECTOR", d.wake_detector),
            wake_phrase=_env_str("AUTOBOT_WAKE_PHRASE", d.wake_phrase).lower(),
            wake_model=_env_str("AUTOBOT_WAKE_MODEL", d.wake_model),
            wake_threshold=_env_float("AUTOBOT_WAKE_THRESHOLD", d.wake_threshold),
            vad_threshold=_env_float("AUTOBOT_VAD_THRESHOLD", d.vad_threshold),
            end_silence_ms=_env_int("AUTOBOT_END_SILENCE_MS", d.end_silence_ms),
            save_audio=_env_bool("AUTOBOT_SAVE_AUDIO", d.save_audio),
            max_utterance_s=_env_float("AUTOBOT_MAX_UTTERANCE_S", d.max_utterance_s),
            wake_preroll_ms=_env_int("AUTOBOT_WAKE_PREROLL_MS", d.wake_preroll_ms),
            follow_up_window_s=_env_float("AUTOBOT_FOLLOWUP_WINDOW_S", d.follow_up_window_s),
            tts_enabled=_env_bool("AUTOBOT_TTS", d.tts_enabled),
            tts_voice=_env_str("AUTOBOT_TTS_VOICE", d.tts_voice),
            speak_acknowledgements=_env_bool("AUTOBOT_ACK", d.speak_acknowledgements),
            allow_web=_env_bool("AUTOBOT_ALLOW_WEB", d.allow_web),
            web_results=_env_int("AUTOBOT_WEB_RESULTS", d.web_results),
            web_provider=_env_str("AUTOBOT_WEB_PROVIDER", d.web_provider),
            web_api_url=_env_str("AUTOBOT_WEB_API_URL", d.web_api_url),
            web_api_key=_env_str("AUTOBOT_WEB_API_KEY", d.web_api_key),
            web_backend=_env_str("AUTOBOT_WEB_BACKEND", d.web_backend),
            session_log=_env_bool("AUTOBOT_SESSION_LOG", d.session_log),
            session_dir=_env_str("AUTOBOT_SESSION_DIR", d.session_dir),
            show_debug=_env_bool("AUTOBOT_DEBUG", d.show_debug),
            log_dir=_env_str("AUTOBOT_LOG_DIR", d.log_dir),
            log_level=_env_str("AUTOBOT_LOG_LEVEL", d.log_level),
            log_console_level=_env_str("AUTOBOT_LOG_CONSOLE_LEVEL", d.log_console_level),
        )
