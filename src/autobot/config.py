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

# --- Defaults -------------------------------------------------------------
# qwen3:8b is the most reliable small tool-caller for 16 GB Apple Silicon.
# Swap to "qwen3:4b" (snappier) or "gemma4:2b" (very constrained) via env.
_DEFAULT_LLM_MODEL = "qwen3:8b"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
# base.en is the English-only starting point on M2; "small.en" for more accuracy.
_DEFAULT_STT_MODEL = "base.en"
# CTranslate2 has no Metal backend, so STT runs on CPU; int8 keeps it light.
_DEFAULT_STT_DEVICE = "cpu"
_DEFAULT_STT_COMPUTE_TYPE = "int8"

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


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime-tunable configuration for the assistant."""

    llm_model: str = _DEFAULT_LLM_MODEL
    ollama_host: str = _DEFAULT_OLLAMA_HOST
    llm_temperature: float = 0.0
    stt_model: str = _DEFAULT_STT_MODEL
    stt_device: str = _DEFAULT_STT_DEVICE
    stt_compute_type: str = _DEFAULT_STT_COMPUTE_TYPE
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
    end_silence_ms: int = 800
    max_utterance_s: float = 15.0
    # Audio kept from just *before* the wake word fires, prepended to the capture
    # so a command spoken in the same breath ("hey jarvis, what's the time") isn't
    # clipped by wake-word detection latency. 0 disables it.
    wake_preroll_ms: int = 400
    # After a turn, keep listening for a follow-up without the wake word for this
    # long; if no speech arrives, re-arm the wake word. 0 disables follow-up mode.
    follow_up_window_s: float = 8.0
    # Logging.
    log_dir: str = _DEFAULT_LOG_DIR
    log_level: str = _DEFAULT_LOG_LEVEL
    log_console_level: str = _DEFAULT_LOG_CONSOLE_LEVEL
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``AUTOBOT_*`` / ``OLLAMA_HOST`` env vars.

        Unset variables fall back to the module defaults. This is the only place
        the environment is read.
        """
        return cls(
            llm_model=_env_str("AUTOBOT_LLM_MODEL", _DEFAULT_LLM_MODEL),
            ollama_host=_env_str("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST),
            llm_temperature=_env_float("AUTOBOT_LLM_TEMPERATURE", 0.0),
            stt_model=_env_str("AUTOBOT_STT_MODEL", _DEFAULT_STT_MODEL),
            stt_device=_env_str("AUTOBOT_STT_DEVICE", _DEFAULT_STT_DEVICE),
            stt_compute_type=_env_str("AUTOBOT_STT_COMPUTE_TYPE", _DEFAULT_STT_COMPUTE_TYPE),
            sandbox_dir=_env_str("AUTOBOT_SANDBOX_DIR", _DEFAULT_SANDBOX_DIR),
            audit_db=_env_str("AUTOBOT_AUDIT_DB", _DEFAULT_AUDIT_DB),
            input_mode=_env_str("AUTOBOT_INPUT", _DEFAULT_INPUT_MODE),
            wake_detector=_env_str("AUTOBOT_WAKE_DETECTOR", _DEFAULT_WAKE_DETECTOR),
            wake_phrase=_env_str("AUTOBOT_WAKE_PHRASE", _DEFAULT_WAKE_PHRASE).lower(),
            wake_model=_env_str("AUTOBOT_WAKE_MODEL", _DEFAULT_WAKE_MODEL),
            wake_threshold=_env_float("AUTOBOT_WAKE_THRESHOLD", 0.3),
            vad_threshold=_env_float("AUTOBOT_VAD_THRESHOLD", 0.5),
            end_silence_ms=_env_int("AUTOBOT_END_SILENCE_MS", 800),
            max_utterance_s=_env_float("AUTOBOT_MAX_UTTERANCE_S", 15.0),
            wake_preroll_ms=_env_int("AUTOBOT_WAKE_PREROLL_MS", 400),
            follow_up_window_s=_env_float("AUTOBOT_FOLLOWUP_WINDOW_S", 8.0),
            log_dir=_env_str("AUTOBOT_LOG_DIR", _DEFAULT_LOG_DIR),
            log_level=_env_str("AUTOBOT_LOG_LEVEL", _DEFAULT_LOG_LEVEL),
            log_console_level=_env_str("AUTOBOT_LOG_CONSOLE_LEVEL", _DEFAULT_LOG_CONSOLE_LEVEL),
        )
