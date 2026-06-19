"""Tests for environment-driven settings."""

from __future__ import annotations

import pytest

from autobot.config import Settings


def test_defaults_are_english_only_and_sensible() -> None:
    settings = Settings()
    assert settings.stt_model.endswith(".en")  # English-only build
    assert settings.llm_model == "qwen3:8b"
    assert settings.llm_max_tokens > 0
    assert settings.sample_rate == 16_000
    assert settings.channels == 1


def test_load_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    from autobot.config import load_env_file

    env = Path(str(tmp_path)) / ".env"
    env.write_text(
        '# a comment\nexport AUTOBOT_WEB_API_KEY="sk-test-123"\nAUTOBOT_ALLOW_WEB=1\n\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AUTOBOT_WEB_API_KEY", raising=False)
    monkeypatch.delenv("AUTOBOT_ALLOW_WEB", raising=False)
    load_env_file(env)
    import os

    assert os.environ["AUTOBOT_WEB_API_KEY"] == "sk-test-123"
    assert os.environ["AUTOBOT_ALLOW_WEB"] == "1"


def test_load_env_file_does_not_override_real_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    from autobot.config import load_env_file

    env = Path(str(tmp_path)) / ".env"
    env.write_text("AUTOBOT_LLM_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("AUTOBOT_LLM_MODEL", "from-real-env")
    load_env_file(env)
    import os

    assert os.environ["AUTOBOT_LLM_MODEL"] == "from-real-env"  # real env wins


def test_from_env_defaults_match_field_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no env vars set, from_env() must equal the dataclass field defaults —
    # guards against the two drifting (the bug where follow_up stayed 8s).
    for key in list(__import__("os").environ):
        if key.startswith("AUTOBOT_") or key == "OLLAMA_HOST":
            monkeypatch.delenv(key, raising=False)
    # Point at a non-existent .env so an ambient one doesn't taint the comparison.
    monkeypatch.setenv("AUTOBOT_ENV_FILE", "/nonexistent/.env")
    assert Settings.from_env() == Settings()


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
