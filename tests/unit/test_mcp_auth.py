"""Unit tests for the pure token-injection helper (no Keychain, no SDK)."""

from __future__ import annotations

from collections.abc import Callable

from autobot.mcp.auth import stdio_env_for
from autobot.mcp.config import McpServerConfig


def _cfg(
    *,
    auth_type: str = "none",
    token_env: str | None = None,
    secret_ref: str | None = None,
    env: dict[str, str] | None = None,
) -> McpServerConfig:
    return McpServerConfig(
        id="test",
        label="Test",
        transport="stdio",
        auth_type=auth_type,
        token_env=token_env,
        secret_ref=secret_ref,
        env=env or {},
    )


def _fake_secret(value: str | None) -> Callable[[str], str | None]:
    """A get_secret stub that always returns ``value`` (typed, no ignores needed)."""

    def getter(name: str) -> str | None:
        return value

    return getter


def test_token_injected_when_all_fields_set() -> None:
    cfg = _cfg(
        auth_type="token",
        token_env="SLACK_BOT_TOKEN",
        secret_ref="mcp.slack.token",
        env={"SLACK_TEAM_ID": "T0123"},
    )
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123", "SLACK_BOT_TOKEN": "xoxb-fake"}


def test_no_token_when_get_secret_returns_none() -> None:
    cfg = _cfg(
        auth_type="token",
        token_env="SLACK_BOT_TOKEN",
        secret_ref="mcp.slack.token",
        env={"SLACK_TEAM_ID": "T0123"},
    )
    result = stdio_env_for(cfg, _fake_secret(None))
    # env is non-empty (SLACK_TEAM_ID), so a dict is returned — but without the token
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_auth_type_none_env_vars_still_returned() -> None:
    cfg = _cfg(auth_type="none", env={"FOO": "bar"})
    result = stdio_env_for(cfg, _fake_secret("ignored"))
    assert result == {"FOO": "bar"}


def test_auth_type_none_ignores_secret_ref() -> None:
    cfg = _cfg(auth_type="none", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token")
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    # auth_type != "token" → secret is never looked up; empty env → None
    assert result is None


def test_token_auth_missing_token_env_skips_injection() -> None:
    # token_env is None → can't inject even if secret is present
    cfg = _cfg(
        auth_type="token",
        token_env=None,
        secret_ref="mcp.slack.token",
        env={"SLACK_TEAM_ID": "T0123"},
    )
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_token_auth_missing_secret_ref_skips_injection() -> None:
    # secret_ref is None → nothing to look up
    cfg = _cfg(
        auth_type="token",
        token_env="SLACK_BOT_TOKEN",
        secret_ref=None,
        env={"SLACK_TEAM_ID": "T0123"},
    )
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_empty_env_and_no_token_returns_none() -> None:
    # Empty cfg.env + auth_type "none" → empty dict → None (inherit parent env)
    cfg = _cfg(auth_type="none")
    assert stdio_env_for(cfg, _fake_secret(None)) is None


def test_empty_env_with_successful_token_injection_returns_dict() -> None:
    # Even with empty cfg.env, a successful token injection produces a non-empty dict
    cfg = _cfg(auth_type="token", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token")
    result = stdio_env_for(cfg, _fake_secret("xoxb-token"))
    assert result == {"SLACK_BOT_TOKEN": "xoxb-token"}
