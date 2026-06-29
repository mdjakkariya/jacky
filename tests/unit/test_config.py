"""Tests for the JSON-backed settings (settings.json > defaults; no env)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autobot.config import Settings, write_settings


def test_defaults_are_english_only_and_sensible() -> None:
    settings = Settings()
    assert settings.stt_model.endswith(".en")  # English-only build
    assert settings.llm_model == "qwen3:8b"
    assert settings.llm_provider == "ollama"  # local by default
    assert settings.llm_max_tokens > 0
    assert settings.sample_rate == 16_000
    assert settings.channels == 1


def test_load_missing_file_is_all_defaults(tmp_path: Path) -> None:
    assert Settings.load(tmp_path / "nope.json") == Settings()


def test_load_overlays_only_present_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"llm_model": "qwen3:4b", "llm_temperature": 0.3, "allow_web": True}, path)
    s = Settings.load(path)
    assert s.llm_model == "qwen3:4b"
    assert s.llm_temperature == 0.3
    assert s.allow_web is True
    assert s.stt_model == Settings().stt_model  # untouched -> default


def test_load_ignores_unknown_keys_and_bad_types(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings(
        {"totally_unknown": 1, "llm_max_tokens": "not-a-number", "stt_beam_size": 7}, path
    )
    s = Settings.load(path)
    assert not hasattr(s, "totally_unknown")
    assert s.llm_max_tokens == Settings().llm_max_tokens  # bad value -> default
    assert s.stt_beam_size == 7  # good value applied


def test_wake_phrase_is_lowercased(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"wake_phrase": "Jack"}, path)
    assert Settings.load(path).wake_phrase == "jack"


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = Settings(llm_provider="anthropic", anthropic_model="claude-x", allow_memory=False)
    write_settings(original.to_dict(), path)
    assert Settings.load(path) == original


def test_malformed_json_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert Settings.load(path) == Settings()


def test_no_secret_fields_are_stored() -> None:
    # API keys belong in the Keychain, never in settings — there must be no key field.
    data = Settings().to_dict()
    assert "web_api_key" not in data
    assert "anthropic_api_key" not in data


def test_write_sets_restrictive_permissions(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"llm_model": "x"}, path)
    assert json.loads(path.read_text())["llm_model"] == "x"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_settings_is_immutable() -> None:
    settings = Settings()
    with pytest.raises(AttributeError):
        settings.llm_model = "other"  # type: ignore[misc]


def test_allow_mcp_defaults_off() -> None:
    assert Settings().allow_mcp is False


def test_allow_mcp_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"allow_mcp": True}, path)
    assert Settings.load(path).allow_mcp is True


def test_tool_selection_defaults() -> None:
    s = Settings()
    assert s.tool_budget == 20
    assert s.tool_selection == "lexical"
    assert s.tool_core_extra == []
    assert s.tool_core_remove == []


def test_tool_selection_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings(
        {
            "tool_budget": 12,
            "tool_selection": "all",
            "tool_core_extra": ["slack__search"],
            "tool_core_remove": ["disk_space"],
        },
        path,
    )
    s = Settings.load(path)
    assert s.tool_budget == 12
    assert s.tool_selection == "all"
    assert s.tool_core_extra == ["slack__search"]
    assert s.tool_core_remove == ["disk_space"]
