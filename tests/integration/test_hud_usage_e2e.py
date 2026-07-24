"""End-to-end: the CLI HUD's context/cost feed over a real ``GET /coder/usage`` socket.

The HUD pulls live context-window usage + session cost at turn-end from ``/coder/usage``
(``cli/shell._refresh_hud_after_turn``), not from the bus ``ContextEvent`` (which only reaches
the orb's WebSocket channel). This spins up a real uvicorn daemon whose ``on_usage`` returns a
representative payload, then drives the real ``client.get_usage`` (urllib) + the real refresh
against a real ``JackApp``, asserting the docked HUD state is populated from the wire.

Skipped unless uvicorn/fastapi (base deps) are installed. Run with:
    uv run pytest tests/integration/test_hud_usage_e2e.py -v
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from autobot.cli import client, shell
from autobot.cli.app import JackApp
from autobot.core.events import EventBus
from autobot.daemon.server import create_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_hud_context_and_cost_flow_from_coder_usage(tmp_path: Any) -> None:
    import uvicorn

    def on_usage() -> dict[str, Any]:
        return {
            "ctx": {"used": 50000, "window": 200000, "model": "opus"},
            "provider": "anthropic",
            "model": "opus",
            "rollups": {"session": {"turns": 1, "usd": 0.34}},
        }

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(EventBus(), on_usage=on_usage)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="hud-e2e-daemon", daemon=True)
    thread.start()

    async def noop(_t: str, _n: int) -> None:
        return None

    try:
        assert _wait(lambda: client.is_daemon_up(base_url), 15.0), "daemon never came up"
        japp = JackApp(
            cwd=str(tmp_path), run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput()
        )
        # The real turn-end refresh: real urllib GET over the socket → real HudState update.
        shell._refresh_hud_after_turn(japp, base_url, str(tmp_path))

        assert japp.hud_state.used == 50000
        assert japp.hud_state.window == 200000
        assert japp.hud_state.model == "opus"
        assert japp.hud_state.cost_usd == 0.34
        # And it renders: the composed status line shows a live context percentage.
        text = "".join(t for _s, t in japp._status_text())
        assert "ctx 25%" in text  # 50000 / 200000
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=10.0)
