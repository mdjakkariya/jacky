"""The localhost WebSocket server that streams engine events to UI clients.

This is the *transport* only — all intelligence stays in the engine. The server
subscribes to an :class:`~autobot.core.events.EventBus` and forwards each event
to every connected client as JSON. Heavy web dependencies (FastAPI / uvicorn) are
imported lazily inside the functions, so importing this module — and the test
suite — never requires the optional ``daemon`` extra.

Privacy: the server binds to ``127.0.0.1`` only. It carries coarse state and a
normalized amplitude scalar — never audio, text, or anything that identifies the
user — and it is reachable only from the local machine.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

# Imported at module top (not lazily) on purpose: with ``from __future__ import
# annotations`` the ``websocket: WebSocket`` hint is a *string*, and FastAPI
# resolves it against this module's globals via ``get_type_hints``. If ``WebSocket``
# were imported inside the function, that resolution fails and FastAPI mistakes
# the parameter for a required query field (closing every connection with 1008).
# This module is the daemon transport, so requiring FastAPI to import it is fine;
# nothing on the core/engine import path pulls this in. uvicorn stays lazy.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from autobot.core.events import EventBus, StateEvent
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.config import Settings

_log = get_logger("daemon")


def create_app(bus: EventBus) -> Any:
    """Build the FastAPI app that serves the event stream.

    Args:
        bus: The hub the engine publishes to; each connection subscribes to it.

    Returns:
        A FastAPI application exposing ``GET /healthz`` and ``WebSocket /ws``.
    """
    app = FastAPI(title="Autobot daemon", docs_url=None, redoc_url=None)

    async def healthz() -> dict[str, str]:
        """Liveness probe for clients/tests."""
        return {"status": "ok", "state": bus.last_state.value}

    async def ws(websocket: WebSocket) -> None:
        """Stream engine events to one client until it disconnects."""
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        def on_event(message: dict[str, object]) -> None:
            # Called from the engine thread — hand off to the loop and return fast.
            loop.call_soon_threadsafe(queue.put_nowait, message)

        unsubscribe = bus.subscribe(on_event)
        _log.info("client connected")
        try:
            # Replay the current state so a late joiner renders correctly at once.
            await websocket.send_json(StateEvent(bus.last_state).message())
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            _log.info("client disconnected")
        finally:
            unsubscribe()

    # Register routes explicitly (rather than via decorators) so the handlers
    # stay statically typed under mypy strict — FastAPI ships no type info here.
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_websocket_route("/ws", ws)
    return app


def run_daemon(bus: EventBus, host: str, port: int) -> None:
    """Run the daemon server (blocking) on ``host:port``.

    Refuses to bind anything other than loopback — the daemon is local-only by
    design, and a non-loopback bind would expose the engine on the network.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"daemon must bind loopback only, got host={host!r}")
    import uvicorn

    _log.info("serving host=%s port=%d", host, port)
    uvicorn.run(create_app(bus), host=host, port=port, log_level="warning")


def daemon_settings(settings: Settings) -> tuple[str, int]:
    """Extract the daemon bind address from settings (small helper for callers)."""
    return settings.daemon_host, settings.daemon_port
