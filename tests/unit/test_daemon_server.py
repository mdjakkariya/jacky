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


def test_run_daemon_refuses_non_loopback_bind() -> None:
    bus = EventBus()
    with pytest.raises(ValueError, match="loopback"):
        run_daemon(bus, "0.0.0.0", 8765)
