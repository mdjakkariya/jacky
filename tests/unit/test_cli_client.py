"""HTTP client helpers for the daemon-backed slash commands (no real network)."""

from __future__ import annotations

from typing import Any

from autobot.cli import client


def test_post_settings_forwards_updates() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True, "applied": sorted(payload)}

    res = client.post_settings("http://x", {"llm_model": "qwen3:8b"}, post=fake_post)
    assert res["ok"] is True
    assert seen["url"] == "http://x/settings"
    assert seen["payload"] == {"llm_model": "qwen3:8b"}


def test_post_settings_transport_error_is_friendly() -> None:
    def boom(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise OSError("down")

    res = client.post_settings("http://x", {"x": 1}, post=boom)
    assert res["ok"] is False and "down" in res["error"]


def test_coder_undo_posts_and_returns_message() -> None:
    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert url == "http://x/coder/undo"
        return {"ok": True, "message": "Reverted to before edit"}

    res = client.coder_undo("http://x", post=fake_post)
    assert res["ok"] is True and "Reverted" in res["message"]


def test_coder_checkpoints_unwraps_list() -> None:
    def fake_get(url: str, timeout: float) -> Any:
        assert url == "http://x/coder/checkpoints"
        return {"checkpoints": [{"ref": "refs/jack/checkpoints/0", "sha": "a", "label": "x"}]}

    rows = client.coder_checkpoints("http://x", get=fake_get)
    assert rows and rows[0]["label"] == "x"


def test_list_sessions_returns_list() -> None:
    def fake_get(url: str, timeout: float) -> Any:
        assert url == "http://x/sessions"
        return [{"id": "abc", "cwd": "/x", "model": "m", "mtime": 0.0}]

    assert client.list_sessions("http://x", get=fake_get)[0]["id"] == "abc"


def test_resume_session_posts_id() -> None:
    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert url == "http://x/sessions/resume" and payload == {"id": "abc"}
        return {"ok": True}

    assert client.resume_session("http://x", "abc", post=fake_post)["ok"] is True


def test_get_models_unwraps() -> None:
    def fake_get(url: str, timeout: float) -> Any:
        return {"models": ["qwen3:8b", "llama3"]}

    assert client.get_models("http://x", get=fake_get) == ["qwen3:8b", "llama3"]


def test_stream_events_parses_task_frames() -> None:
    def fake_open(url: str):  # type: ignore[no-untyped-def]
        assert url == "http://x/coder/events"
        yield ": ping"  # keepalive comment — ignored
        yield 'data: {"type": "task", "id": "task-1", "status": "done"}'
        yield ""
        yield 'data: {"type": "task", "id": "task-2", "status": "failed"}'

    events = list(client.stream_events("http://x", open_stream=fake_open))
    assert [e["id"] for e in events] == ["task-1", "task-2"]
    assert events[0]["status"] == "done"


def test_stream_events_empty_on_transport_error() -> None:
    def boom(url: str):  # type: ignore[no-untyped-def]
        raise OSError("stream dropped")
        yield  # pragma: no cover - unreachable, makes this a generator

    assert list(client.stream_events("http://x", open_stream=boom)) == []
