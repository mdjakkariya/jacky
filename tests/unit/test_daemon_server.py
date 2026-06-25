"""Tests for the daemon WebSocket server.

Skipped automatically when the optional ``daemon`` extra (FastAPI) is absent, so
the core test run stays dependency-light while CI with the extra exercises it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from autobot.core.events import EventBus, OrbState
from autobot.daemon.server import create_app, run_daemon


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
