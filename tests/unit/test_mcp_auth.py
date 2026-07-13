"""Unit tests for the pure token-injection helper (no Keychain, no SDK)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from autobot.mcp.auth import (
    OAUTH_CALLBACK_PORT,
    KeychainTokenStorage,
    LoopbackCallbackServer,
    oauth_redirect_uri,
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


class _FakeKeyring:
    """In-memory keyring backend keyed by account name.

    Mirrors the real ``keyring`` API subset ``autobot.secrets`` uses:
    ``get_password``/``set_password``/``delete_password``. Seeded directly via
    ``store`` for pre-populated cases.
    """

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[name] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.store[name]


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


class _FakeClientInfoFull:
    """Stand-in for OAuthClientInformationFull with full constructor kwargs support.

    Mirrors the kwargs accepted by the real ``OAuthClientInformationFull`` so tests
    can patch ``mcp.shared.auth.OAuthClientInformationFull`` with this class and
    verify the arguments passed by ``KeychainTokenStorage.get_client_info()``.
    """

    def __init__(
        self,
        *,
        redirect_uris: list[object],
        client_id: str,
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "none",
        grant_types: list[str] | None = None,
        response_types: list[str] | None = None,
        client_name: str | None = None,
    ) -> None:
        self.redirect_uris = redirect_uris
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self.grant_types = grant_types
        self.response_types = response_types
        self.client_name = client_name

    def model_dump_json(self) -> str:
        return json.dumps({"client_id": self.client_id})

    @classmethod
    def model_validate(cls, data: object) -> _FakeClientInfoFull:
        assert isinstance(data, dict)
        return cls(redirect_uris=[], client_id=data["client_id"])


def test_keychain_get_tokens_returns_none_when_absent() -> None:
    """get_tokens returns None when Keychain has no entry."""
    store: dict[str, str] = {}
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)
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


def test_keychain_token_value_never_logged_on_read(caplog: pytest.LogCaptureFixture) -> None:
    """get_tokens must not emit the stored token value into any log record."""
    store: dict[str, str] = {
        "mcp.github.oauth": json.dumps(
            {"access_token": "super-secret-tok-read-7777", "token_type": "Bearer"}
        )
    }
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage("github", backend=backend)

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
        result = asyncio.run(storage.get_tokens())

    assert result is not None
    for record in caplog.records:
        assert "super-secret-tok-read-7777" not in record.getMessage(), (
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


def test_open_browser_raises_for_data_scheme() -> None:
    """open_browser raises ValueError for data: URLs (XSS/exfil vector)."""
    with pytest.raises(ValueError, match="data"):
        open_browser("data:text/html,<script>alert(1)</script>")


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


def test_loopback_fixed_port_stored() -> None:
    """LoopbackCallbackServer(port=N) stores the fixed port in _fixed_port without binding."""
    server = LoopbackCallbackServer(port=12345)
    assert server._fixed_port == 12345


def test_loopback_default_port_zero() -> None:
    """LoopbackCallbackServer() stores 0 as the default fixed port."""
    server = LoopbackCallbackServer()
    assert server._fixed_port == 0


def test_loopback_reclaims_fixed_port_from_abandoned_server() -> None:
    """A second fixed-port start() reclaims the port from an abandoned prior flow.

    Regression: a previous OAuth attempt that didn't complete left the fixed port
    bound, so a retry hit OSError(EADDRINUSE). The new flow now closes the prior
    fixed-port server before binding.
    """
    import socket

    import autobot.mcp.auth as auth_mod

    # Pick a currently-free port to use as the "fixed" port (avoids hardcoding 8975).
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    fixed = probe.getsockname()[1]
    probe.close()

    async def _run() -> str:
        first = LoopbackCallbackServer(port=fixed)
        await first.start()  # binds `fixed`; abandoned (never wait()ed → never closed)
        second = LoopbackCallbackServer(port=fixed)
        try:
            return await second.start()  # must reclaim the port, not raise EADDRINUSE
        finally:
            for srv in (second._server, first._server):
                if srv is not None:
                    srv.close()

    try:
        uri = asyncio.run(_run())
        assert uri == f"http://127.0.0.1:{fixed}/callback"
    finally:
        auth_mod._active_fixed_server = None  # reset module state for other tests


# ---------------------------------------------------------------------------
# OAUTH_CALLBACK_PORT and oauth_redirect_uri
# ---------------------------------------------------------------------------


def test_oauth_callback_port_constant() -> None:
    """OAUTH_CALLBACK_PORT is 8975."""
    assert OAUTH_CALLBACK_PORT == 8975


def test_oauth_redirect_uri_format() -> None:
    """oauth_redirect_uri returns the expected fixed redirect URI."""
    assert oauth_redirect_uri() == "http://127.0.0.1:8975/callback"


# ---------------------------------------------------------------------------
# KeychainTokenStorage — pre-registered client path
# ---------------------------------------------------------------------------


def test_keychain_get_client_info_returns_preregistered_when_client_id_set() -> None:
    """get_client_info returns a pre-built client when client_id is configured."""
    store: dict[str, str] = {}
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage(
        "slack",
        backend=backend,
        client_id="my-client-id",
        client_secret="my-secret",
        redirect_uri="http://127.0.0.1:8975/callback",
    )

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthClientInformationFull=_FakeClientInfoFull),
        },
    ):
        result = asyncio.run(storage.get_client_info())

    assert result is not None
    assert isinstance(result, _FakeClientInfoFull)
    assert result.client_id == "my-client-id"
    assert result.client_secret == "my-secret"
    assert result.token_endpoint_auth_method == "client_secret_post"
    # No Keychain read should have occurred for the client key
    assert "mcp.slack.client" not in store


def test_keychain_get_client_info_auth_method_none_when_no_secret() -> None:
    """get_client_info uses token_endpoint_auth_method='none' when no client_secret."""
    store: dict[str, str] = {}
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage(
        "github",
        backend=backend,
        client_id="gh-client-id",
        client_secret=None,
        redirect_uri="http://127.0.0.1:8975/callback",
    )

    with patch.dict(
        "sys.modules",
        {
            "mcp": MagicMock(),
            "mcp.shared": MagicMock(),
            "mcp.shared.auth": MagicMock(OAuthClientInformationFull=_FakeClientInfoFull),
        },
    ):
        result = asyncio.run(storage.get_client_info())

    assert result is not None
    assert isinstance(result, _FakeClientInfoFull)
    assert result.client_id == "gh-client-id"
    assert result.token_endpoint_auth_method == "none"


def test_keychain_set_client_info_noop_when_client_id_set() -> None:
    """set_client_info does NOT write to Keychain when client_id is configured."""
    store: dict[str, str] = {}
    backend = _FakeKeyring(store)
    storage = KeychainTokenStorage(
        "slack",
        backend=backend,
        client_id="my-client-id",
        client_secret="my-secret",
        redirect_uri="http://127.0.0.1:8975/callback",
    )
    fake_info = _FakeClientInfoFull(
        redirect_uris=[],
        client_id="dcr-assigned-id",
        token_endpoint_auth_method="none",
    )

    asyncio.run(storage.set_client_info(fake_info))

    # The pre-registered client key must NOT be written
    assert "mcp.slack.client" not in store
