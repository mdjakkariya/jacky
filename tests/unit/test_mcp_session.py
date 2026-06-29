"""Unit tests for the pure parts of the MCP session worker (no SDK, no subprocess)."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from autobot.mcp import adapter
from autobot.mcp.approvals import load_approvals, record_fingerprints
from autobot.mcp.config import McpServerConfig
from autobot.mcp.session import McpServerWorker, _Call, friendly_error, tool_allowed
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


def test_friendly_error_plain_exception_passthrough() -> None:
    assert friendly_error(RuntimeError("boom")) == "boom"


def test_friendly_error_unwraps_exception_group() -> None:
    grp = BaseExceptionGroup("g", [ValueError("real cause")])
    assert friendly_error(grp) == "real cause"


def test_friendly_error_translates_oauth_registration_failure() -> None:
    # GitHub-style: anyio wraps an OAuthRegistrationError in a TaskGroup group.
    grp = BaseExceptionGroup("tg", [RuntimeError("Registration failed: 404 page not found")])
    msg = friendly_error(grp).lower()
    assert "personal access token" in msg
    assert "registration failed" not in msg  # raw text replaced with guidance


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
    # DCR path: no pre-registered client_id
    assert storage._client_id is None


def test_build_oauth_provider_pre_registered_client(
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """_build_oauth_provider wires client_id + client_secret for pre-registered OAuth apps."""
    pytest.importorskip("mcp")  # skip if mcp extra absent
    from mcp.client.auth import OAuthClientProvider

    from autobot.mcp.auth import OAUTH_CALLBACK_PORT, KeychainTokenStorage, LoopbackCallbackServer

    cfg = McpServerConfig(
        id="slack",
        label="Slack",
        transport="http",
        auth_type="oauth",
        client_id="slack-client-id-abc",
        url="https://mcp.slack.com/mcp",
    )
    worker = McpServerWorker(cfg, ToolRegistry(), loop=worker_loop)

    captured: list[LoopbackCallbackServer] = []

    async def _fake_start(self: LoopbackCallbackServer) -> str:
        captured.append(self)  # capture the server to verify it was given the fixed port
        return "http://127.0.0.1:8975/callback"

    with (
        patch.object(LoopbackCallbackServer, "start", _fake_start),
        patch("autobot.mcp.session._get_secret", return_value="slack-secret-xyz"),
    ):
        provider = asyncio.run(worker._build_oauth_provider())

    assert isinstance(provider, OAuthClientProvider)
    storage = provider.context.storage
    assert isinstance(storage, KeychainTokenStorage)
    assert storage._client_id == "slack-client-id-abc"
    assert storage._client_secret == "slack-secret-xyz"
    # The redirect URI flows into storage, and the callback server uses the fixed port
    # the user registers (so the pre-registered redirect_uri matches).
    assert storage._redirect_uri == "http://127.0.0.1:8975/callback"
    assert captured[0]._fixed_port == OAUTH_CALLBACK_PORT


# ---------------------------------------------------------------------------
# Fake helpers for fingerprint/_sync_tools tests (no MCP SDK required)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAnnotations:
    readOnlyHint: bool | None = None  # noqa: N815
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815


@dataclass
class _FakeTool:
    name: str
    description: str | None = None
    inputSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815
    annotations: Any = None


@dataclass
class _FakeListed:
    """Mimics the object returned by ``session.list_tools()``."""

    tools: list[_FakeTool]


class _FakeSession:
    """Async-capable fake that satisfies ``session.list_tools()`` calls in _sync_tools."""

    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools

    async def list_tools(self) -> _FakeListed:
        """Return a fake ListToolsResult with the preconfigured tool list."""
        return _FakeListed(tools=list(self._tools))


def _make_sync_worker(
    loop: asyncio.AbstractEventLoop,
    server_id: str = "srv",
    approvals_path: Path | None = None,
    on_event: Any = None,
) -> McpServerWorker:
    """Build a minimal McpServerWorker for _sync_tools tests."""
    cfg = McpServerConfig(id=server_id, label=server_id, transport="stdio")
    kwargs: dict[str, Any] = {"loop": loop}
    if on_event is not None:
        kwargs["on_event"] = on_event
    if approvals_path is not None:
        kwargs["approvals_path"] = approvals_path
    return McpServerWorker(cfg, ToolRegistry(), **kwargs)


# ---------------------------------------------------------------------------
# _sync_tools fingerprint tests
# ---------------------------------------------------------------------------


def test_sync_tools_unchanged_fingerprint_registers_tool(tmp_path: Path) -> None:
    """When a tool's fingerprint matches approved.json, the tool is registered."""
    approval_path = tmp_path / "approved.json"
    tool = _FakeTool(
        name="search",
        description="Search the web",
        inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    reg_name = adapter.namespaced("srv", tool.name)
    fp = adapter.fingerprint(tool)
    # Pre-seed approved.json with the correct fingerprint
    record_fingerprints("srv", {reg_name: fp}, approval_path)

    events: list[dict[str, Any]] = []
    loop = asyncio.new_event_loop()
    try:
        worker = _make_sync_worker(loop, approvals_path=approval_path, on_event=events.append)
        session = _FakeSession([tool])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    # Tool is registered
    assert reg_name in worker._registered
    # No mcp_tool_changed event
    assert not any(e.get("type") == "mcp_tool_changed" for e in events)
    # all_tools entry has pending_reconsent False
    at = {t["name"]: t for t in worker.all_tools()}
    assert at["search"]["pending_reconsent"] is False


def test_sync_tools_changed_fingerprint_blocks_and_emits_event(tmp_path: Path) -> None:
    """Fingerprint change blocks registration and emits mcp_tool_changed event."""
    approval_path = tmp_path / "approved.json"
    tool = _FakeTool(
        name="deploy",
        description="Deploy service",
        inputSchema={"type": "object", "properties": {}},
    )
    reg_name = adapter.namespaced("srv", tool.name)
    # Pre-seed with a DIFFERENT fingerprint (rug-pull scenario)
    record_fingerprints("srv", {reg_name: "completely_wrong_fingerprint"}, approval_path)

    events: list[dict[str, Any]] = []
    loop = asyncio.new_event_loop()
    try:
        worker = _make_sync_worker(loop, approvals_path=approval_path, on_event=events.append)
        session = _FakeSession([tool])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    # Tool is NOT registered
    assert reg_name not in worker._registered
    # mcp_tool_changed event was emitted
    changed_events = [e for e in events if e.get("type") == "mcp_tool_changed"]
    assert len(changed_events) == 1
    assert changed_events[0]["server"] == "srv"
    assert reg_name in changed_events[0]["tools"]
    # all_tools entry has pending_reconsent True
    at = {t["name"]: t for t in worker.all_tools()}
    assert at["deploy"]["pending_reconsent"] is True


def test_sync_tools_new_tool_registers_and_writes_fingerprint(tmp_path: Path) -> None:
    """New tool (absent from approved.json) is registered and its fingerprint baselined."""
    approval_path = tmp_path / "approved.json"
    tool = _FakeTool(
        name="greet",
        description="Say hello",
        inputSchema={"type": "object", "properties": {"name": {"type": "string"}}},
    )
    reg_name = adapter.namespaced("srv", tool.name)
    expected_fp = adapter.fingerprint(tool)

    events: list[dict[str, Any]] = []
    loop = asyncio.new_event_loop()
    try:
        worker = _make_sync_worker(loop, approvals_path=approval_path, on_event=events.append)
        session = _FakeSession([tool])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    # Tool is registered
    assert reg_name in worker._registered
    # Fingerprint was written to approved.json
    loaded = load_approvals(approval_path)
    assert loaded.fingerprints.get("srv", {}).get(reg_name) == expected_fp
    # No mcp_tool_changed event (new tool is auto-approved)
    assert not any(e.get("type") == "mcp_tool_changed" for e in events)
    # pending_reconsent is False for new tool
    at = {t["name"]: t for t in worker.all_tools()}
    assert at["greet"]["pending_reconsent"] is False


def test_sync_tools_denied_tool_not_fingerprinted(tmp_path: Path) -> None:
    """Denied tools are skipped before fingerprinting; no approved.json entry written."""
    approval_path = tmp_path / "approved.json"
    tool = _FakeTool(name="admin_delete", description="Delete everything")
    reg_name = adapter.namespaced("srv", tool.name)

    cfg = McpServerConfig(id="srv", label="srv", transport="stdio", tool_deny=("admin_*",))
    loop = asyncio.new_event_loop()
    try:
        worker = McpServerWorker(cfg, ToolRegistry(), loop=loop, approvals_path=approval_path)
        session = _FakeSession([tool])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    # Tool is not registered
    assert reg_name not in worker._registered
    # No fingerprint written
    loaded = load_approvals(approval_path)
    assert reg_name not in loaded.fingerprints.get("srv", {})


def test_sync_tools_all_tools_retains_existing_keys_plus_pending_reconsent(
    tmp_path: Path,
) -> None:
    """all_tools() entries keep all prior keys and gain pending_reconsent on every entry."""
    approval_path = tmp_path / "approved.json"
    tool = _FakeTool(name="info", description="Get info")

    loop = asyncio.new_event_loop()
    try:
        worker = _make_sync_worker(loop, approvals_path=approval_path)
        session = _FakeSession([tool])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    entries = worker.all_tools()
    assert len(entries) == 1
    entry = entries[0]
    # All prior keys must be present
    for key in ("name", "description", "risk", "network", "enabled"):
        assert key in entry, f"Missing key: {key}"
    # New key must be present
    assert "pending_reconsent" in entry


def test_sync_tools_mixed_scenario(tmp_path: Path) -> None:
    """Mixed unchanged/rug-pull/new: only the changed tool is blocked and in the event."""
    approval_path = tmp_path / "approved.json"
    tool_ok = _FakeTool(name="list_files", description="List files")
    tool_changed = _FakeTool(name="write_file", description="Write a file")
    tool_new = _FakeTool(name="delete_file", description="Delete a file")

    reg_ok = adapter.namespaced("srv", tool_ok.name)
    reg_changed = adapter.namespaced("srv", tool_changed.name)
    reg_new = adapter.namespaced("srv", tool_new.name)

    # Seed: ok has matching fp; changed has wrong fp; new is absent
    record_fingerprints(
        "srv",
        {
            reg_ok: adapter.fingerprint(tool_ok),
            reg_changed: "stale_wrong_fingerprint",
        },
        approval_path,
    )

    events: list[dict[str, Any]] = []
    loop = asyncio.new_event_loop()
    try:
        worker = _make_sync_worker(loop, approvals_path=approval_path, on_event=events.append)
        session = _FakeSession([tool_ok, tool_changed, tool_new])
        asyncio.run(worker._sync_tools(session))
    finally:
        loop.close()

    assert reg_ok in worker._registered
    assert reg_new in worker._registered
    assert reg_changed not in worker._registered

    changed_events = [e for e in events if e.get("type") == "mcp_tool_changed"]
    assert len(changed_events) == 1
    assert reg_changed in changed_events[0]["tools"]
    assert reg_ok not in changed_events[0]["tools"]

    at = {t["name"]: t for t in worker.all_tools()}
    assert at["list_files"]["pending_reconsent"] is False
    assert at["write_file"]["pending_reconsent"] is True
    assert at["delete_file"]["pending_reconsent"] is False

    # New tool's fingerprint was written; changed tool's old fingerprint is preserved
    loaded = load_approvals(approval_path)
    assert loaded.fingerprints["srv"][reg_new] == adapter.fingerprint(tool_new)
    assert loaded.fingerprints["srv"][reg_changed] == "stale_wrong_fingerprint"


# ---------------------------------------------------------------------------
# Fake confirmers for spawn-consent tests
# ---------------------------------------------------------------------------


class _AlwaysAllowConfirmer:
    """Confirmer that approves every prompt and records the calls it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.calls.append((prompt, kind))
        return True

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        return default


class _AlwaysDenyConfirmer:
    """Confirmer that rejects every prompt."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.calls.append((prompt, kind))
        return False

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        return ""


class _NeverCallConfirmer:
    """Confirmer that raises AssertionError if called — proves confirm was NOT invoked."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        raise AssertionError("confirm() should NOT have been called")

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        raise AssertionError("choose() should NOT have been called")


def _make_consent_worker(
    loop: asyncio.AbstractEventLoop,
    approvals_path: Path,
    confirmer: Any = None,
    command: str = "uvx",
    args: tuple[str, ...] = ("mcp-server-fetch",),
    server_id: str = "test-server",
) -> McpServerWorker:
    """Build a McpServerWorker configured for spawn-consent tests."""
    cfg = McpServerConfig(
        id=server_id,
        label=server_id,
        transport="stdio",
        command=command,
        args=args,
    )
    kwargs: dict[str, Any] = {
        "loop": loop,
        "approvals_path": approvals_path,
    }
    if confirmer is not None:
        kwargs["confirmer"] = confirmer
    return McpServerWorker(cfg, ToolRegistry(), **kwargs)


# ---------------------------------------------------------------------------
# _check_spawn_consent tests
# ---------------------------------------------------------------------------


def test_spawn_consent_existing_matching_approval_returns_true_without_calling_confirmer(
    tmp_path: Path,
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """Pre-seeded matching approval → True returned, confirmer never invoked."""
    approval_path = tmp_path / "approved.json"
    from autobot.mcp.approvals import record_spawn_approval

    record_spawn_approval("test-server", "uvx", ["mcp-server-fetch"], approval_path)

    never_call = _NeverCallConfirmer()
    worker = _make_consent_worker(worker_loop, approval_path, confirmer=never_call)
    result = asyncio.run(worker._check_spawn_consent())
    assert result is True


def test_spawn_consent_no_approval_always_allow_returns_true_and_writes_approval(
    tmp_path: Path,
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """No prior approval + AlwaysAllow confirmer → True; approval persisted."""
    approval_path = tmp_path / "approved.json"
    confirmer = _AlwaysAllowConfirmer()
    worker = _make_consent_worker(worker_loop, approval_path, confirmer=confirmer)

    result = asyncio.run(worker._check_spawn_consent())

    assert result is True
    assert len(confirmer.calls) == 1  # confirm was called exactly once
    loaded = load_approvals(approval_path)
    sa = loaded.spawn_approvals.get("test-server")
    assert sa is not None
    assert sa.command == "uvx"
    assert sa.args == ["mcp-server-fetch"]


def test_spawn_consent_no_approval_always_deny_returns_false_and_no_approval_written(
    tmp_path: Path,
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """No prior approval + AlwaysDeny confirmer → False; no approval written."""
    approval_path = tmp_path / "approved.json"
    confirmer = _AlwaysDenyConfirmer()
    worker = _make_consent_worker(worker_loop, approval_path, confirmer=confirmer)

    result = asyncio.run(worker._check_spawn_consent())

    assert result is False
    assert len(confirmer.calls) == 1
    loaded = load_approvals(approval_path)
    assert loaded.spawn_approvals.get("test-server") is None


def test_spawn_consent_changed_args_re_prompts_and_updates_approval(
    tmp_path: Path,
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """Existing approval with DIFFERENT args → re-prompts; approval updated on allow."""
    approval_path = tmp_path / "approved.json"
    from autobot.mcp.approvals import record_spawn_approval

    # Pre-seed with old args
    record_spawn_approval("test-server", "uvx", ["old-server-arg"], approval_path)

    confirmer = _AlwaysAllowConfirmer()
    # Worker has NEW args (different from seeded)
    worker = _make_consent_worker(
        worker_loop, approval_path, confirmer=confirmer, args=("mcp-server-fetch",)
    )

    result = asyncio.run(worker._check_spawn_consent())

    assert result is True
    assert len(confirmer.calls) == 1  # re-prompted because args changed
    loaded = load_approvals(approval_path)
    sa = loaded.spawn_approvals.get("test-server")
    assert sa is not None
    assert sa.args == ["mcp-server-fetch"]  # updated to new args


def test_spawn_consent_no_confirmer_headless_auto_approves_and_writes(
    tmp_path: Path,
    worker_loop: asyncio.AbstractEventLoop,
) -> None:
    """confirmer=None with no prior approval → auto-approves and writes approval."""
    approval_path = tmp_path / "approved.json"
    # Pass no confirmer (headless path)
    worker = _make_consent_worker(worker_loop, approval_path, confirmer=None)

    result = asyncio.run(worker._check_spawn_consent())

    assert result is True
    loaded = load_approvals(approval_path)
    sa = loaded.spawn_approvals.get("test-server")
    assert sa is not None
    assert sa.command == "uvx"
    assert sa.args == ["mcp-server-fetch"]


# ---------------------------------------------------------------------------
# McpManager.set_confirmer test
# ---------------------------------------------------------------------------


def test_mcp_manager_set_confirmer_stores_confirmer() -> None:
    """set_confirmer() wires the confirmer into _confirmer attribute."""
    from autobot.mcp.manager import McpManager

    manager = McpManager({}, ToolRegistry())
    confirmer = _AlwaysAllowConfirmer()
    manager.set_confirmer(confirmer)
    assert manager._confirmer is confirmer
