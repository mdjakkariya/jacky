"""The /coder/usage route serves the on_usage payload; the client parses it."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from autobot.cli.client import get_usage
from autobot.core.events import EventBus
from autobot.daemon.server import create_app


def _payload() -> dict[str, Any]:
    return {
        "ctx": {"model": "claude-sonnet-5", "used": 1400, "window": 1_000_000},
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "rollups": {
            "totals": {},
            "daily": [],
            "by_model": [],
            "by_provider": [],
            "by_workspace": [],
            "session": None,
        },
    }


def test_usage_route_returns_on_usage_payload() -> None:
    app = create_app(EventBus(), on_usage=lambda: _payload())
    client = TestClient(app)
    resp = client.get("/coder/usage")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "anthropic"


def test_usage_route_empty_when_unwired() -> None:
    client = TestClient(create_app(EventBus()))
    assert client.get("/coder/usage").json() == {}


def test_client_get_usage_returns_empty_on_transport_error() -> None:
    # Nothing listening on this port -> {} (no traceback).
    assert get_usage("http://127.0.0.1:9") == {}
