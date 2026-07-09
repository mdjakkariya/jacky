"""Pure config parsing/validation for the ``jack config`` CLI."""

from __future__ import annotations

import pytest

from autobot.cli.config_ops import (
    ConfigError,
    coerce_value,
    format_settings,
    prepare_set,
    resolve_key,
    validate,
)
from autobot.config import Settings

D = Settings()


def test_resolve_aliases() -> None:
    assert resolve_key("provider", provider="ollama") == "llm_provider"
    assert resolve_key("autonomy", provider="ollama") == "coding_autonomy"


def test_resolve_model_is_provider_aware() -> None:
    assert resolve_key("model", provider="anthropic") == "anthropic_model"
    assert resolve_key("model", provider="ollama") == "llm_model"
    assert resolve_key("model", provider="openai") == "llm_model"


def test_resolve_raw_key_passthrough() -> None:
    assert resolve_key("coder_llm_max_tokens", provider="ollama") == "coder_llm_max_tokens"


def test_resolve_unknown_key_raises_with_hint() -> None:
    with pytest.raises(ConfigError) as e:
        resolve_key("provdr", provider="ollama")
    assert "unknown setting" in str(e.value).lower()


def test_coerce_bool() -> None:
    assert coerce_value("checkpoints", "false", defaults=D) is False
    assert coerce_value("checkpoints", "YES", defaults=D) is True
    with pytest.raises(ConfigError):
        coerce_value("checkpoints", "maybe", defaults=D)


def test_coerce_int_positivity() -> None:
    assert coerce_value("coder_llm_max_tokens", "8192", defaults=D) == 8192
    with pytest.raises(ConfigError):
        coerce_value("coder_llm_max_tokens", "0", defaults=D)  # budget must be > 0
    with pytest.raises(ConfigError):
        coerce_value("coder_llm_max_tokens", "abc", defaults=D)


def test_coerce_list_and_str_empty_rules() -> None:
    assert coerce_value("command_allowlist", "git *, pytest*", defaults=D) == ["git *", "pytest*"]
    assert coerce_value("command_allowlist", "", defaults=D) == []
    # openai_base_url default is "" so empty is allowed (means default endpoint)
    assert coerce_value("openai_base_url", "", defaults=D) == ""


def test_validate_enums() -> None:
    validate("llm_provider", "anthropic")  # no raise
    with pytest.raises(ConfigError):
        validate("llm_provider", "gpt")
    with pytest.raises(ConfigError):
        validate("coding_autonomy", "yolo")


def test_prepare_set_end_to_end() -> None:
    key, val = prepare_set("provider", "anthropic", current={}, defaults=D)
    assert (key, val) == ("llm_provider", "anthropic")
    # model resolves against the *current* provider
    key, val = prepare_set(
        "model", "claude-sonnet-5", current={"llm_provider": "anthropic"}, defaults=D
    )
    assert key == "anthropic_model" and val == "claude-sonnet-5"
    with pytest.raises(ConfigError):
        prepare_set("provider", "nope", current={}, defaults=D)


def test_format_settings_masks_secrets() -> None:
    out = format_settings(
        {"llm_provider": "anthropic", "_secrets": {"anthropic_api_key": True}},
        {"anthropic_api_key": True, "openai_api_key": False},
    )
    assert "llm_provider = anthropic" in out
    assert "_secrets" not in out  # internal key not shown as a setting
    assert "anthropic_api_key: set" in out
    assert "openai_api_key: unset" in out
