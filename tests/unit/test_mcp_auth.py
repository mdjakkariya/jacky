"""Unit tests for the pure token-injection helper (no Keychain, no SDK)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from autobot.mcp.auth import (
    KeychainTokenStorage,
    LoopbackCallbackServer,
    open_browser,
    stdio_env_for,
)
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


# ---------------------------------------------------------------------------
# Phase 6: KeychainTokenStorage tests
# ---------------------------------------------------------------------------


def _make_fake_runner(store: dict[str, str]) -> Callable[[list[str]], tuple[int, str]]:
    """Create a fake Keychain runner that stores values in ``store`` (in-memory)."""

    def runner(args: list[str]) -> tuple[int, str]:
        # Simulate: security find-generic-password -s autobot -a <name> -w
        if "find-generic-password" in args:
            try:
                idx = args.index("-a")
                name = args[idx + 1]
            except (ValueError, IndexError):
                return 1, "missing -a flag"
            if name in store:
                return 0, store[name]
            return 1, "not found"
        # Simulate: security add-generic-password -U -s autobot -a <name> -w <value>
        if "add-generic-password" in args:
            try:
                idx_a = args.index("-a")
                idx_w = args.index("-w")
                name = args[idx_a + 1]
                value = args[idx_w + 1]
            except (ValueError, IndexError):
                return 1, "missing flags"
            store[name] = value
            return 0, ""
        return 1, "unknown command"

    return runner


class _FakeOAuthToken:
    """Minimal stand-in for mcp.shared.auth.OAuthToken (no mcp extra needed)."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def model_dump_json(self) -> str:
        return json.dumps({"access_token": self.access_token, "token_type": "Bearer"})

    @classmethod
    def model_validate(cls, data: object) -> _FakeOAuthToken:
        assert isinstance(data, dict)
        return cls(data["access_token"])


class _FakeClientInfo:
    """Minimal stand-in for mcp.shared.auth.OAuthClientInformationFull."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    def model_dump_json(self) -> str:
        return json.dumps({"client_id": self.client_id})

    @classmethod
    def model_validate(cls, data: object) -> _FakeClientInfo:
        assert isinstance(data, dict)
        return cls(data["client_id"])


def test_keychain_get_tokens_returns_none_when_absent() -> None:
    """get_tokens returns None when Keychain has no entry."""
    store: dict[str, str] = {}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthToken=_FakeOAuthToken),
        },
    ):
        result = asyncio.run(storage.get_tokens())

    assert result is None


def test_keychain_set_and_get_tokens_round_trip() -> None:
    """set_tokens then get_tokens returns the same access_token."""
    store: dict[str, str] = {}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    fake_token = _FakeOAuthToken("tok-abc123")

    async def _run() -> object | None:
        with patch.dict(
            "sys.modules",
            {
                "mcp": MagicMock(),
                "mcp.shared": MagicMock(),
                "mcp.shared.auth": MagicMock(OAuthToken=_FakeOAuthToken),
            },
        ):
            await storage.set_tokens(fake_token)
            return await storage.get_tokens()

    result = asyncio.run(_run())

    assert result is not None
    assert isinstance(result, _FakeOAuthToken)
    assert result.access_token == "tok-abc123"


def test_keychain_get_tokens_returns_none_on_unparseable_json() -> None:
    """get_tokens returns None when the stored JSON is corrupt."""
    store: dict[str, str] = {"mcp.github.oauth": "not-valid-json!!!"}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthToken=_FakeOAuthToken),
        },
    ):
        result = asyncio.run(storage.get_tokens())

    assert result is None


def test_keychain_get_client_info_returns_none_when_absent() -> None:
    """get_client_info returns None when Keychain has no entry."""
    store: dict[str, str] = {}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthClientInformationFull=_FakeClientInfo),
        },
    ):
        result = asyncio.run(storage.get_client_info())

    assert result is None


def test_keychain_set_and_get_client_info_round_trip() -> None:
    """set_client_info then get_client_info returns the same client_id."""
    store: dict[str, str] = {}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    fake_info = _FakeClientInfo("client-xyz")

    async def _run() -> object | None:
        with patch.dict(
            "sys.modules",
            {
                "mcp": MagicMock(),
                "mcp.shared": MagicMock(),
                "mcp.shared.auth": MagicMock(OAuthClientInformationFull=_FakeClientInfo),
            },
        ):
            await storage.set_client_info(fake_info)
            return await storage.get_client_info()

    result = asyncio.run(_run())

    assert result is not None
    assert isinstance(result, _FakeClientInfo)
    assert result.client_id == "client-xyz"


def test_keychain_get_client_info_returns_none_on_unparseable_json() -> None:
    """get_client_info returns None when the stored JSON is corrupt."""
    store: dict[str, str] = {"mcp.github.client": "{bad json"}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthClientInformationFull=_FakeClientInfo),
        },
    ):
        result = asyncio.run(storage.get_client_info())

    assert result is None


def test_keychain_token_value_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """set_tokens must not emit the token value into any log record."""
    store: dict[str, str] = {}
    runner = _make_fake_runner(store)
    storage = KeychainTokenStorage("github", runner=runner)
    fake_token = _FakeOAuthToken("super-secret-tok-9999")

    with (
        caplog.at_level(logging.DEBUG),
        patch.dict(
            "sys.modules",
            {
                "mcp": MagicMock(),
                "mcp.shared": MagicMock(),
                "mcp.shared.auth": MagicMock(OAuthToken=_FakeOAuthToken),
            },
        ),
    ):
        asyncio.run(storage.set_tokens(fake_token))

    for record in caplog.records:
        assert "super-secret-tok-9999" not in record.getMessage(), (
            f"Token value leaked in log record: {record.getMessage()}"
        )


# ---------------------------------------------------------------------------
# Phase 6: open_browser tests
# ---------------------------------------------------------------------------


def test_open_browser_raises_for_file_scheme() -> None:
    """open_browser raises ValueError for file:// URLs."""
    with pytest.raises(ValueError, match="file"):
        open_browser("file:///etc/passwd")


def test_open_browser_raises_for_javascript_scheme() -> None:
    """open_browser raises ValueError for javascript: URLs."""
    with pytest.raises(ValueError, match="javascript"):
        open_browser("javascript:alert(1)")


def test_open_browser_raises_for_custom_scheme() -> None:
    """open_browser raises ValueError for custom URI schemes."""
    with pytest.raises(ValueError, match="myapp"):
        open_browser("myapp://oauth/callback")


def test_open_browser_calls_webbrowser_for_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_browser calls webbrowser.open for valid https URLs."""
    calls: list[str] = []

    def _fake_open(url: str) -> bool:
        calls.append(url)
        return True

    monkeypatch.setattr("webbrowser.open", _fake_open)
    open_browser("https://example.com/authorize?client_id=abc")
    assert calls == ["https://example.com/authorize?client_id=abc"]


def test_open_browser_calls_webbrowser_for_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_browser calls webbrowser.open for valid http URLs (e.g. localhost)."""
    calls: list[str] = []

    def _fake_open(url: str) -> bool:
        calls.append(url)
        return True

    monkeypatch.setattr("webbrowser.open", _fake_open)
    open_browser("http://localhost:8080/authorize")
    assert calls == ["http://localhost:8080/authorize"]


# ---------------------------------------------------------------------------
# Phase 6: LoopbackCallbackServer tests
# ---------------------------------------------------------------------------


def test_loopback_captures_code_and_state() -> None:
    """LoopbackCallbackServer returns (code, state) from a real loopback GET."""
    import urllib.request

    async def _run() -> tuple[str, str | None]:
        server = LoopbackCallbackServer(timeout=5.0)
        redirect_uri = await server.start()
        # Issue the callback GET in the background (same event loop, different task).
        callback_url = redirect_uri + "?code=auth-code-xyz&state=csrf-token-abc"

        async def _do_get() -> None:
            # Short delay to ensure server.wait() is already awaiting.
            await asyncio.sleep(0.05)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, urllib.request.urlopen, callback_url)

        get_task = asyncio.create_task(_do_get())
        code, state = await server.wait()
        await get_task
        return code, state

    code, state = asyncio.run(_run())
    assert code == "auth-code-xyz"
    assert state == "csrf-token-abc"


def test_loopback_captures_code_without_state() -> None:
    """LoopbackCallbackServer returns (code, None) when state is absent."""
    import urllib.request

    async def _run() -> tuple[str, str | None]:
        server = LoopbackCallbackServer(timeout=5.0)
        redirect_uri = await server.start()
        callback_url = redirect_uri + "?code=no-state-code"

        async def _do_get() -> None:
            await asyncio.sleep(0.05)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, urllib.request.urlopen, callback_url)

        get_task = asyncio.create_task(_do_get())
        code, state = await server.wait()
        await get_task
        return code, state

    code, state = asyncio.run(_run())
    assert code == "no-state-code"
    assert state is None


def test_loopback_times_out() -> None:
    """LoopbackCallbackServer raises asyncio.TimeoutError when no callback arrives."""

    async def _run() -> None:
        server = LoopbackCallbackServer(timeout=0.1)
        await server.start()
        await server.wait()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run())
