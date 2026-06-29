"""Unit tests for the pure parts of the MCP session worker (no SDK, no subprocess)."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from autobot.mcp.config import McpServerConfig
from autobot.mcp.session import McpServerWorker, _Call, tool_allowed
from autobot.tools.registry import ToolRegistry


@pytest.fixture
def worker_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A fresh event loop for worker construction, closed on teardown.

    These tests never *run* the loop (``_http_headers`` is pure; ``_build_oauth_provider``
    runs under its own ``asyncio.run``), but ``McpServerWorker`` requires one — so we
    hand it a real loop and close it here to avoid leaking a ``ResourceWarning``.
    """
    lp = asyncio.new_event_loop()
    try:
        yield lp
    finally:
        lp.close()


def _make_worker(
    loop: asyncio.AbstractEventLoop,
    transport: str = "stdio",
    auth_type: str = "none",
    secret_ref: str | None = None,
    url: str | None = None,
    server_id: str = "test-server",
) -> McpServerWorker:
    """Construct a McpServerWorker with minimal config for unit tests."""
    cfg = McpServerConfig(
        id=server_id,
        label=server_id,
        transport=transport,
        auth_type=auth_type,
        secret_ref=secret_ref,
        url=url,
    )
    return McpServerWorker(cfg, ToolRegistry(), loop=loop)


def test_tool_allowed_empty_allow_permits_all() -> None:
    assert tool_allowed("anything", (), ()) is True


def test_tool_allowed_allow_glob_filters() -> None:
    assert tool_allowed("slack_send", ("slack_*",), ()) is True
    assert tool_allowed("github_pr", ("slack_*",), ()) is False


def test_tool_allowed_deny_glob_wins_over_allow() -> None:
    assert tool_allowed("slack_admin_delete", ("slack_*",), ("*_delete",)) is False


def test_tool_allowed_deny_without_allow() -> None:
    assert tool_allowed("dangerous", (), ("dang*",)) is False
    assert tool_allowed("safe", (), ("dang*",)) is True


def test_fail_pending_resolves_queued_calls_fast() -> None:
    # On worker exit, any still-queued call must be failed immediately so its caller
    # doesn't block for the full CALL_TIMEOUT_S.
    loop = asyncio.new_event_loop()
    try:
        cfg = McpServerConfig(id="s", label="s", transport="stdio")
        worker = McpServerWorker(cfg, ToolRegistry(), loop=loop)
        worker._queue = asyncio.Queue()
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        worker._queue.put_nowait(_Call(tool="t", args={}, future=future))
        worker._fail_pending()
        assert future.done()
        assert isinstance(future.exception(), RuntimeError)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _http_headers tests (no SDK needed)
# ---------------------------------------------------------------------------


def test_http_headers_returns_bearer_when_token_present(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_http_headers returns Authorization header when auth_type=='token' and secret present."""
    worker = _make_worker(worker_loop, auth_type="token", secret_ref="my-secret-ref")
    with patch("autobot.mcp.session._get_secret", return_value="my-token") as mock_gs:
        headers = worker._http_headers()
    mock_gs.assert_called_once_with("my-secret-ref")
    assert headers == {"Authorization": "Bearer my-token"}


def test_http_headers_returns_empty_when_token_missing(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_http_headers returns {} when auth_type=='token' but secret lookup returns None."""
    worker = _make_worker(worker_loop, auth_type="token", secret_ref="my-secret-ref")
    with patch("autobot.mcp.session._get_secret", return_value=None):
        headers = worker._http_headers()
    assert headers == {}


def test_http_headers_returns_empty_when_no_secret_ref(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_http_headers returns {} when auth_type=='token' but no secret_ref configured."""
    worker = _make_worker(worker_loop, auth_type="token", secret_ref=None)
    headers = worker._http_headers()
    assert headers == {}


def test_http_headers_returns_empty_for_auth_type_none(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_http_headers returns {} when auth_type=='none'."""
    worker = _make_worker(worker_loop, auth_type="none")
    headers = worker._http_headers()
    assert headers == {}


def test_http_headers_returns_empty_for_auth_type_oauth(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_http_headers returns {} when auth_type=='oauth' (bearer headers not used for OAuth)."""
    worker = _make_worker(worker_loop, auth_type="oauth", url="https://example.com")
    headers = worker._http_headers()
    assert headers == {}


# ---------------------------------------------------------------------------
# _build_oauth_provider test (needs mcp SDK)
# ---------------------------------------------------------------------------


def test_build_oauth_provider_returns_correct_type_with_keychain_storage(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_build_oauth_provider returns OAuthClientProvider wired to KeychainTokenStorage."""
    mcp = pytest.importorskip("mcp")  # noqa: F841 — skip if mcp extra absent
    from mcp.client.auth import OAuthClientProvider

    from autobot.mcp.auth import KeychainTokenStorage, LoopbackCallbackServer

    worker = _make_worker(
        worker_loop,
        transport="http",
        auth_type="oauth",
        url="https://example.com/mcp",
        server_id="my-mcp-server",
    )

    # Stub start() so the test doesn't bind a real loopback socket (it's never awaited
    # to completion here, so the dangling server would leak). The real bind path is
    # covered by test_mcp_auth.py's LoopbackCallbackServer tests.
    async def _fake_start(self: LoopbackCallbackServer) -> str:
        return "http://127.0.0.1:0/callback"

    with patch.object(LoopbackCallbackServer, "start", _fake_start):
        provider = asyncio.run(worker._build_oauth_provider())

    assert isinstance(provider, OAuthClientProvider)
    # OAuthClientProvider stores its args in provider.context (OAuthContext dataclass)
    storage = provider.context.storage
    assert isinstance(storage, KeychainTokenStorage)
    # KeychainTokenStorage uses "mcp.<server_id>.oauth" as the token key
    assert storage._token_key == "mcp.my-mcp-server.oauth"
