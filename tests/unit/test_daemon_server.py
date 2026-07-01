"""Tests for the daemon WebSocket server.

Skipped automatically when the optional ``daemon`` extra (FastAPI) is absent, so
the core test run stays dependency-light while CI with the extra exercises it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from autobot.core.events import EventBus, OrbState
from autobot.daemon.server import create_app, run_daemon

if TYPE_CHECKING:
    from autobot.mcp.manager import McpManager


def test_healthz_reports_current_state() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))
    bus.publish_state(OrbState.THINKING)
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "state": "thinking"}


def test_ws_replays_current_state_then_streams_events() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "state", "value": "idle"}
        bus.publish_state(OrbState.LISTENING)
        bus.publish_amplitude(0.42)
        assert ws.receive_json() == {"type": "state", "value": "listening"}
        assert ws.receive_json() == {"type": "amplitude", "value": 0.42}


def test_ws_forwards_visibility_frames() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "state", "value": "idle"}  # replay
        bus.publish_visibility(visible=False)
        assert ws.receive_json() == {"type": "visibility", "value": "hide"}


def test_run_daemon_refuses_non_loopback_bind() -> None:
    bus = EventBus()
    with pytest.raises(ValueError, match="loopback"):
        run_daemon(bus, "0.0.0.0", 8765)


def test_meeting_post_routes_dispatch_to_recorder() -> None:
    """POST /meeting/{start,stop,pause,resume} must reach on_meeting (not 422).

    Regression: registering these as ``lambda r: post_meeting("stop", r)`` made
    FastAPI treat the untyped ``r`` as a required query param, so every POST 422'd
    and the recorder was never called (Stop/Pause buttons did nothing).
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def on_meeting(action: str, payload: dict[str, Any]) -> str:
        calls.append((action, payload))
        return "ok"

    client = TestClient(create_app(EventBus(), on_meeting=on_meeting))
    # Bodiless POSTs must succeed (the bug 422'd them on a missing query param).
    for path in ("/meeting/stop", "/meeting/pause", "/meeting/resume"):
        resp = client.post(path)
        assert resp.status_code == 200, (path, resp.status_code, resp.text)
        assert resp.json()["ok"] is True
    resp = client.post("/meeting/start", json={"title": "Sync"})
    assert resp.status_code == 200
    assert ("start", {"title": "Sync"}) in calls
    assert {"stop", "pause", "resume"} <= {a for a, _ in calls}


def test_meeting_post_routes_report_disabled_when_no_recorder() -> None:
    client = TestClient(create_app(EventBus()))  # on_meeting=None
    resp = client.post("/meeting/stop")
    assert resp.status_code == 200
    assert resp.json().get("ok") is False


def test_meeting_reveal_route_passes_id_to_recorder() -> None:
    """POST /meeting/reveal forwards the JSON body's id to on_meeting('reveal', ...)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def on_meeting(action: str, payload: dict[str, Any]) -> object:
        calls.append((action, payload))
        return {"ok": True, "dir": "/x/meetings/abc"}

    client = TestClient(create_app(EventBus(), on_meeting=on_meeting))
    resp = client.post("/meeting/reveal", json={"id": "2026-07-01-1508-standup"})
    assert resp.status_code == 200
    assert ("reveal", {"id": "2026-07-01-1508-standup"}) in calls


def _settings_client(tmp_path: object) -> TestClient:
    from pathlib import Path

    path = Path(str(tmp_path)) / "settings.json"
    return TestClient(create_app(EventBus(), settings_path=path))


def test_get_settings_returns_config_and_secret_flags(tmp_path: object) -> None:
    body = _settings_client(tmp_path).get("/settings").json()
    assert body["llm_provider"] == "ollama"  # default
    assert "_secrets" in body
    assert set(body["_secrets"]) == {"anthropic_api_key", "web_api_key"}


def test_post_settings_persists_and_ignores_unknown_keys(tmp_path: object) -> None:
    client = _settings_client(tmp_path)
    resp = client.post(
        "/settings", json={"llm_provider": "anthropic", "bogus": 1, "allow_memory": False}
    ).json()
    assert resp["ok"] is True
    assert resp["applied"] == ["allow_memory", "llm_provider"]
    # Reflected on read, and the unknown key was dropped.
    body = client.get("/settings").json()
    assert body["llm_provider"] == "anthropic"
    assert body["allow_memory"] is False
    assert "bogus" not in body


def test_setup_reports_needs_setup_until_settings_saved(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "settings.json"
    client = TestClient(create_app(EventBus(), settings_path=path))
    body = client.get("/setup").json()
    assert body["needs_setup"] is True  # no settings file yet -> first run
    assert "has_anthropic_key" in body and "voice_present" in body
    # Saving any setting writes the file -> no longer a first run.
    client.post("/settings", json={"llm_provider": "anthropic"})
    assert client.get("/setup").json()["needs_setup"] is False


def test_get_models_returns_a_list(tmp_path: object) -> None:
    # No Ollama running in the test env -> empty list, but the shape is stable.
    body = _settings_client(tmp_path).get("/models").json()
    assert isinstance(body["models"], list)


def test_post_secret_rejects_unknown_name(tmp_path: object) -> None:
    resp = _settings_client(tmp_path).post("/secret", json={"name": "evil", "value": "x"}).json()
    assert resp["ok"] is False


def test_post_confirm_delivers_clicked_answer() -> None:
    answers: list[str] = []
    app = create_app(EventBus(), on_confirm_answer=answers.append)
    client = TestClient(app)
    # Legacy bool maps to yes/no; a value (e.g. access level) passes through.
    assert client.post("/confirm", json={"answer": True}).json() == {"ok": True}
    assert client.post("/confirm", json={"answer": False}).json() == {"ok": True}
    assert client.post("/confirm", json={"value": "write"}).json() == {"ok": True}
    assert answers == ["yes", "no", "write"]


def test_voice_status_returns_model_presence_shape() -> None:
    body = TestClient(create_app(EventBus())).get("/voice/status").json()
    assert set(body["models"]) >= {"voice", "stt", "wake"}
    assert isinstance(body["ready"], bool) and "needed" in body


def test_voice_download_starts_and_streams_done(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import time

    import autobot.voice_setup as vs

    # No network: fake the orchestration so the worker thread just reports progress.
    monkeypatch.setattr(vs, "download_missing", lambda s, cb, *a, **k: cb(0.5, "Downloading…"))
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    client = TestClient(create_app(bus))

    assert client.post("/voice/download").json() == {"ok": True, "started": True}
    for _ in range(100):  # wait for the background thread to publish the final frame
        if any(m.get("type") == "voice_download" and m.get("done") for m in seen):
            break
        time.sleep(0.02)
    frames = [m for m in seen if m.get("type") == "voice_download"]
    assert frames and frames[-1]["done"] is True and frames[-1]["stage"] == "Ready"


def test_post_new_session_invokes_callback() -> None:
    calls = {"n": 0}
    app = create_app(EventBus(), on_new_session=lambda: calls.__setitem__("n", calls["n"] + 1))
    assert TestClient(app).post("/session/new").json() == {"ok": True}
    assert calls["n"] == 1


def test_post_new_session_ok_without_callback() -> None:
    # The route must not error when no engine callback is wired (e.g. demo mode).
    assert TestClient(create_app(EventBus())).post("/session/new").json() == {"ok": True}


def test_post_action_runs_tool_through_callback() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def on_action(tool: str, args: dict[str, object]) -> str:
        calls.append((tool, args))
        return f"ran {tool}"

    app = create_app(EventBus(), on_action=on_action)
    body = (
        TestClient(app)
        .post("/action", json={"tool": "open_path", "args": {"path": "~/a.pdf"}})
        .json()
    )
    assert body == {"ok": True, "result": "ran open_path"}
    assert calls == [("open_path", {"path": "~/a.pdf"})]


def test_access_endpoints_list_grant_revoke(tmp_path: object) -> None:
    from pathlib import Path

    from autobot.tools.access import AccessPolicy, set_active_policy

    base = Path(str(tmp_path))
    set_active_policy(AccessPolicy(base / "access.json", base / "ws"))
    try:
        proj = base / "proj"
        proj.mkdir()
        client = TestClient(create_app(EventBus()))
        assert client.get("/access").json() == {"grants": []}
        granted = client.post("/access/grant", json={"path": str(proj), "write": True}).json()
        assert granted["ok"] is True and granted["grant"]["mode"] == "write"
        listed = client.get("/access").json()["grants"]
        assert listed[0]["path"] == str(proj.resolve())
        assert client.post("/access/revoke", json={"path": str(proj)}).json() == {
            "ok": True,
            "removed": True,
        }
        assert client.get("/access").json() == {"grants": []}
    finally:
        set_active_policy(None)  # don't leak the global into other tests


def test_access_grant_rejects_protected_path(tmp_path: object) -> None:
    from pathlib import Path

    from autobot.tools.access import AccessPolicy, set_active_policy

    base = Path(str(tmp_path))
    set_active_policy(AccessPolicy(base / "access.json", base / "ws"))
    try:
        secret = base / ".ssh"
        secret.mkdir()
        resp = (
            TestClient(create_app(EventBus()))
            .post("/access/grant", json={"path": str(secret)})
            .json()
        )
        assert resp["ok"] is False
    finally:
        set_active_policy(None)


def test_post_action_rejects_missing_tool() -> None:
    app = create_app(EventBus(), on_action=lambda t, a: "x")
    assert TestClient(app).post("/action", json={"args": {}}).json()["ok"] is False


def test_post_confirm_rejects_missing_answer() -> None:
    app = create_app(EventBus(), on_confirm_answer=lambda _a: None)
    body = TestClient(app).post("/confirm", json={}).json()
    assert body["ok"] is False


def test_on_change_fires_on_settings_save(tmp_path: object) -> None:
    from pathlib import Path

    calls = {"n": 0}
    path = Path(str(tmp_path)) / "settings.json"
    app = create_app(
        EventBus(), settings_path=path, on_change=lambda: calls.__setitem__("n", calls["n"] + 1)
    )
    client = TestClient(app)
    client.post("/settings", json={"llm_provider": "anthropic"})
    assert calls["n"] == 1  # engine notified to reload, no restart needed


def test_get_workspace_reports_cwd(tmp_path: object) -> None:
    from pathlib import Path

    from autobot.tools.access import AccessPolicy, set_active_policy

    base = Path(str(tmp_path))
    ws = base / "workspace"
    ws.mkdir()
    set_active_policy(AccessPolicy(base / "access.json", ws))
    try:
        resp = TestClient(create_app(EventBus())).get("/workspace")
        assert resp.status_code == 200
        body = resp.json()
        assert "path" in body and "name" in body and "grants" in body
        assert body["name"] == "workspace"
        assert body["path"] == str(ws.resolve())
        assert isinstance(body["grants"], list)
    finally:
        set_active_policy(None)


def test_get_workspace_returns_empty_when_no_policy() -> None:
    resp = TestClient(create_app(EventBus())).get("/workspace")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"path": "", "name": "", "grants": []}


def test_post_workspace_routes_through_on_action() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def on_action(tool: str, args: dict[str, object]) -> str:
        calls.append((tool, args))
        return "ok"

    client = TestClient(create_app(EventBus(), on_action=on_action))
    resp = client.post("/workspace", json={"path": "/some/dir"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "result": "ok"}
    assert calls == [("set_working_directory", {"path": "/some/dir"})]


def test_post_workspace_fails_without_on_action() -> None:
    resp = TestClient(create_app(EventBus())).post("/workspace", json={"path": "/some/dir"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False


def test_ws_sends_last_workspace_on_connect() -> None:
    bus = EventBus()
    bus.publish_workspace("/home/user/project", "project")
    client = TestClient(create_app(bus))
    with client.websocket_connect("/ws") as ws:
        state_frame = ws.receive_json()
        assert state_frame == {"type": "state", "value": "idle"}
        workspace_frame = ws.receive_json()
        assert workspace_frame == {
            "type": "workspace",
            "path": "/home/user/project",
            "name": "project",
        }


def test_post_secret_accepts_mcp_namespace(tmp_path: object) -> None:
    from unittest.mock import patch

    client = _settings_client(tmp_path)
    # Patch set_secret so we don't actually touch the Keychain
    with patch("autobot.secrets.set_secret", return_value=True):
        resp = client.post("/secret", json={"name": "mcp.slack.token", "value": "xoxb-fake"}).json()
    assert resp["ok"] is True


def test_post_secret_accepts_any_mcp_subkey(tmp_path: object) -> None:
    from unittest.mock import patch

    client = _settings_client(tmp_path)
    with patch("autobot.secrets.set_secret", return_value=True):
        resp = client.post("/secret", json={"name": "mcp.github.oauth", "value": "gho-fake"}).json()
    assert resp["ok"] is True


def test_post_secret_still_rejects_arbitrary_names(tmp_path: object) -> None:
    resp = (
        _settings_client(tmp_path)
        .post("/secret", json={"name": "totally_evil", "value": "x"})
        .json()
    )
    assert resp["ok"] is False
    assert "mcp." in resp["error"]  # error message mentions the mcp namespace


def test_post_secret_rejects_bare_mcp_prefix(tmp_path: object) -> None:
    # "mcp." alone (no sub-key) is not a valid secret name
    resp = _settings_client(tmp_path).post("/secret", json={"name": "mcp.", "value": "x"}).json()
    assert resp["ok"] is False


# ---------------------------------------------------------------------------
# Task 5: /mcp/* endpoint tests with a FakeMcp stub
# ---------------------------------------------------------------------------


class _FakeMcp:
    """Minimal McpManager stub for daemon endpoint tests (no SDK, no subprocess)."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {
            "echo": {
                "server": "echo",
                "label": "Echo",
                "enabled": True,
                "egress": "local",
                "state": "connected",
                "tool_count": 2,
                "auth_type": "none",
                "secret_ref": None,
            }
        }

    def status(self) -> list[dict[str, Any]]:
        return list(self._servers.values())

    def secret_present(self, server_id: str) -> bool:
        cfg = self._servers.get(server_id, {})
        return cfg.get("secret_ref") is not None

    def add_or_update_server(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        sid = descriptor.get("id", "")
        transport = descriptor.get("transport", "")
        if transport not in {"stdio", "http"}:
            return {"ok": False, "error": "invalid transport"}
        self._servers[str(sid)] = {
            "server": sid,
            "label": sid,
            "enabled": False,
            "egress": "local",
            "state": "disconnected",
            "tool_count": 0,
            "auth_type": "none",
            "secret_ref": None,
        }
        return {"ok": True, "server": self._servers[str(sid)]}

    def remove_server(self, server_id: str) -> bool:
        return self._servers.pop(server_id, None) is not None

    def set_enabled(self, server_id: str, enabled: bool) -> bool:
        if server_id not in self._servers:
            return False
        self._servers[server_id]["enabled"] = enabled
        return True

    def connect(self, server_id: str) -> None:
        if server_id in self._servers:
            self._servers[server_id]["state"] = "connected"

    def tools_for(self, server_id: str) -> list[dict[str, Any]]:
        if server_id not in self._servers:
            return []
        return [
            {
                "name": "echo",
                "description": "Echo text",
                "risk": "read_only",
                "network": False,
                "enabled": True,
            },
            {
                "name": "whoami",
                "description": "Return token",
                "risk": "read_only",
                "network": False,
                "enabled": True,
            },
        ]

    def set_tool_override(
        self,
        server_id: str,
        tool: str,
        *,
        risk: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        return server_id in self._servers

    def start_oauth(self, server_id: str) -> dict[str, Any]:
        if server_id not in self._servers:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        return {"ok": True, "started": True}

    def shutdown(self) -> None:
        return None


def _provider_for(mcp: _FakeMcp) -> Any:
    """Wrap a fake manager in a real (already-enabled) McpProvider."""
    from autobot.mcp.provider import McpProvider

    provider = McpProvider(lambda: cast("McpManager", mcp))
    provider.set_enabled(True)
    return provider


def _mcp_client(mcp: _FakeMcp) -> TestClient:
    bus = EventBus()
    return TestClient(create_app(bus, mcp_provider=_provider_for(mcp)))


def test_mcp_list_returns_servers() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is True
    servers = resp["servers"]
    assert len(servers) == 1
    assert servers[0]["server"] == "echo"
    assert "auth_type" in servers[0]
    assert "secret_present" in servers[0]


def test_mcp_list_when_disabled_returns_error() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))  # no mcp_provider → disabled
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is False
    assert "mcp disabled" in resp["error"]


def test_post_settings_toggles_mcp_at_runtime(tmp_path: object) -> None:
    """Flipping allow_mcp via /settings enables/disables MCP with no restart."""
    from pathlib import Path

    from autobot.mcp.provider import McpProvider

    fake = _FakeMcp()
    provider = McpProvider(lambda: cast("McpManager", fake))  # starts disabled
    path = Path(str(tmp_path)) / "settings.json"
    path.write_text("{}", encoding="utf-8")
    client = TestClient(create_app(EventBus(), settings_path=path, mcp_provider=provider))

    # Disabled until allow_mcp is set: /mcp/* returns the disabled error.
    assert client.get("/mcp/servers").json()["ok"] is False
    assert provider.manager is None

    # Enabling via settings spins the manager up live.
    assert client.post("/settings", json={"allow_mcp": True}).json()["ok"] is True
    assert provider.manager is fake
    assert client.get("/mcp/servers").json()["ok"] is True

    # Disabling tears it down again.
    assert client.post("/settings", json={"allow_mcp": False}).json()["ok"] is True
    assert provider.manager is None
    assert client.get("/mcp/servers").json()["ok"] is False


def test_mcp_add_valid_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.post(
        "/mcp/servers",
        json={
            "id": "gh",
            "label": "GitHub",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "enabled": False,
        },
    ).json()
    assert resp["ok"] is True
    assert "gh" in fake._servers


def test_mcp_add_invalid_descriptor() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers", json={"id": "bad", "transport": "grpc"}).json()
    assert resp["ok"] is False


def test_mcp_delete_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.delete("/mcp/servers/echo").json()
    assert resp["ok"] is True
    assert "echo" not in fake._servers


def test_mcp_delete_unknown_server() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.delete("/mcp/servers/nonexistent").json()
    assert resp["ok"] is False


def test_mcp_enable_server() -> None:
    fake = _FakeMcp()
    fake._servers["echo"]["enabled"] = False
    client = _mcp_client(fake)
    resp = client.post("/mcp/servers/echo/enable").json()
    assert resp["ok"] is True
    assert fake._servers["echo"]["enabled"] is True


def test_mcp_disable_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.post("/mcp/servers/echo/disable").json()
    assert resp["ok"] is True
    assert fake._servers["echo"]["enabled"] is False


def test_mcp_connect_server() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/connect").json()
    assert resp["ok"] is True


def test_mcp_test_server_returns_status_row() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/test").json()
    assert resp["ok"] is True
    assert resp["server"]["server"] == "echo"


def test_mcp_get_tools() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers/echo/tools").json()
    assert resp["ok"] is True
    assert len(resp["tools"]) == 2
    assert resp["tools"][0]["name"] == "echo"


def test_mcp_set_tool_override() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post(
        "/mcp/servers/echo/tools/echo",
        json={"risk": "write", "enabled": False},
    ).json()
    assert resp["ok"] is True


def test_mcp_auth_start_passthrough() -> None:
    """Endpoint passes the manager's start_oauth return value through unchanged."""
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/auth/start").json()
    assert resp == {"ok": True, "started": True}


def test_mcp_auth_start_unknown_server_passthrough() -> None:
    """Endpoint forwards the manager's error for an unknown server id."""
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/no-such-srv/auth/start").json()
    assert resp["ok"] is False
    assert "unknown" in resp["error"]


def test_mcp_auth_start_when_disabled() -> None:
    """When MCP is disabled the endpoint returns the standard mcp-disabled error."""
    bus = EventBus()
    client = TestClient(create_app(bus))  # no mcp= → disabled
    resp = client.post("/mcp/servers/echo/auth/start").json()
    assert resp["ok"] is False
    assert "mcp disabled" in resp["error"]


def test_mcp_tool_override_accepts_valid_risk() -> None:
    """Valid risk values (read_only, write, destructive) are forwarded to the manager."""
    for valid_risk in ("read_only", "write", "destructive"):
        client = _mcp_client(_FakeMcp())
        resp = client.post(
            "/mcp/servers/echo/tools/echo",
            json={"risk": valid_risk},
        ).json()
        assert resp["ok"] is True, f"expected ok=True for risk={valid_risk!r}, got {resp}"


def test_mcp_tool_override_rejects_invalid_risk() -> None:
    """An unrecognised risk string must be rejected before reaching the manager."""
    client = _mcp_client(_FakeMcp())
    resp = client.post(
        "/mcp/servers/echo/tools/echo",
        json={"risk": "superuser"},
    ).json()
    assert resp["ok"] is False
    assert "invalid risk" in resp["error"]


def test_mcp_tool_override_no_risk_is_accepted() -> None:
    """Omitting risk (None) must still succeed — only enable/disable is adjusted."""
    client = _mcp_client(_FakeMcp())
    resp = client.post(
        "/mcp/servers/echo/tools/echo",
        json={"enabled": False},
    ).json()
    assert resp["ok"] is True


def test_mcp_disabled_all_endpoints_return_error() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))  # no mcp
    for method, path in [
        ("POST", "/mcp/servers"),
        ("DELETE", "/mcp/servers/x"),
        ("POST", "/mcp/servers/x/enable"),
        ("POST", "/mcp/servers/x/disable"),
        ("POST", "/mcp/servers/x/connect"),
        ("POST", "/mcp/servers/x/test"),
        ("GET", "/mcp/servers/x/tools"),
        ("POST", "/mcp/servers/x/tools/t"),
        ("POST", "/mcp/servers/x/auth/start"),
    ]:
        if method == "GET":
            resp = client.get(path).json()
        else:
            resp = client.request(method, path, json={}).json()
        assert resp["ok"] is False, f"{method} {path} should return ok=False when mcp disabled"


def test_mcp_list_includes_secret_present_false_when_no_ref() -> None:
    """Servers without a secret_ref report secret_present=False."""
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is True
    echo = next(s for s in resp["servers"] if s["server"] == "echo")
    assert echo["secret_present"] is False


def test_mcp_list_includes_secret_present_true_when_ref_set() -> None:
    """Servers with a secret_ref and a set Keychain entry report secret_present=True."""

    class _FakeMcpWithRef(_FakeMcp):
        def __init__(self) -> None:
            super().__init__()
            self._servers["echo"]["secret_ref"] = "mcp.echo.token"

        def secret_present(self, server_id: str) -> bool:
            cfg_data = self._servers.get(server_id, {})
            return cfg_data.get("secret_ref") is not None

    client = _mcp_client(_FakeMcpWithRef())
    resp = client.get("/mcp/servers").json()
    echo = next(s for s in resp["servers"] if s["server"] == "echo")
    assert echo["secret_present"] is True
