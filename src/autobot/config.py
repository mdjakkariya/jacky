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
        )
