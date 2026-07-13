"""Daemon /coder/undo + /coder/checkpoints routes (no real engine)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from autobot.core.events import EventBus
from autobot.daemon.server import create_app


def _client(**kw: object) -> TestClient:
    app = create_app(EventBus(), **kw)  # type: ignore[arg-type]
    return TestClient(app)


def test_undo_returns_ok_and_message() -> None:
    c = _client(on_coder_undo=lambda: (True, "Reverted to before edit"))
    r = c.post("/coder/undo", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Reverted to before edit"}


def test_undo_unavailable_without_callback() -> None:
    c = _client()
    r = c.post("/coder/undo", json={})
    assert r.json()["ok"] is False


def test_checkpoints_wraps_list() -> None:
    rows = [{"ref": "refs/jack/checkpoints/0", "sha": "a", "label": "x"}]
    c = _client(on_coder_checkpoints=lambda: rows)
    r = c.get("/coder/checkpoints")
    assert r.json() == {"checkpoints": rows}
