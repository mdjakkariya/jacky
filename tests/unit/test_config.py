"""Tests for environment-driven settings."""

from __future__ import annotations

import pytest

from autobot.config import Settings


def test_defaults_are_english_only_and_sensible() -> None:
    settings = Settings()
    assert settings.stt_model.endswith(".en")  # English-only build
    assert settings.llm_model == "qwen3:8b"
    assert settings.sample_rate == 16_000
    assert settings.channels == 1


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOBOT_LLM_MODEL", "qwen3:4b")
    monkeypatch.setenv("AUTOBOT_STT_MODEL", "small.en")
    monkeypatch.setenv("AUTOBOT_LLM_TEMPERATURE", "0.3")
    settings = Settings.from_env()
    assert settings.llm_model == "qwen3:4b"
    assert settings.stt_model == "small.en"
    assert settings.llm_temperature == 0.3


def test_from_env_bad_float_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOBOT_LLM_TEMPERATURE", "not-a-number")
    assert Settings.from_env().llm_temperature == 0.0


def test_settings_is_immutable() -> None:
    settings = Settings()
    with pytest.raises(AttributeError):
        settings.llm_model = "other"  # type: ignore[misc]
