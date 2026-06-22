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
import contextlib
from typing import TYPE_CHECKING, Any

# Imported at module top (not lazily) on purpose: with ``from __future__ import
# annotations`` the ``websocket: WebSocket`` hint is a *string*, and FastAPI
# resolves it against this module's globals via ``get_type_hints``. If ``WebSocket``
# were imported inside the function, that resolution fails and FastAPI mistakes
# the parameter for a required query field (closing every connection with 1008).
# This module is the daemon transport, so requiring FastAPI to import it is fine;
# nothing on the core/engine import path pulls this in. uvicorn stays lazy.
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

from autobot.core.events import EventBus, StateEvent
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from autobot.config import Settings

_log = get_logger("daemon")

# Secrets the Settings view may store (Keychain account names). Anything else is
# rejected so the endpoint can't write arbitrary Keychain items.
_SECRET_NAMES = ("anthropic_api_key", "web_api_key")


def _installed_ollama_models(host: str) -> list[str]:
    """List local Ollama model names via its `/api/tags` (empty list on any error)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception:  # ollama down / unreachable — just an empty list
        return []
    return sorted(m["name"] for m in data.get("models", []) if isinstance(m, dict) and "name" in m)


def create_app(
    bus: EventBus,
    settings_path: str | Path | None = None,
    on_change: Any | None = None,
    on_confirm_answer: Any | None = None,
) -> Any:
    """Build the FastAPI app: the event stream plus the Settings-view API.

    Args:
        bus: The hub the engine publishes to; each connection subscribes to it.
        settings_path: Override the settings.json path (tests). Defaults to the
            standard ``~/.autobot/settings.json``.
        on_change: Optional callback invoked after a settings/secret change so the
            engine can reload live (the orchestrator's ``mark_llm_dirty``).
        on_confirm_answer: Optional callback (bool) invoked when the user clicks
            Yes/No on a confirmation card, delivering the answer to the engine.

    Returns:
        A FastAPI app: ``/healthz``, WebSocket ``/ws``, the settings API, and
        ``POST /confirm`` (a clicked confirmation answer).
    """
    from fastapi.middleware.cors import CORSMiddleware

    from autobot.config import (
        DEFAULT_SETTINGS_PATH,
        Settings,
        read_settings,
        setting_names,
        write_settings,
    )

    path = settings_path or DEFAULT_SETTINGS_PATH

    app = FastAPI(title="Autobot daemon", docs_url=None, redoc_url=None)
    # The Settings view (Tauri webview / localhost) is a different origin, so it
    # needs CORS. Restrict to local/Tauri origins — the daemon is loopback-only.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(tauri://.*|https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?)$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Normal disconnect, or the task being cancelled on Ctrl-C shutdown.
            _log.info("client disconnected")
        except Exception:  # never let a send error surface as an ASGI crash
            _log.debug("ws closed on error", exc_info=True)
        finally:
            unsubscribe()

    async def get_settings() -> dict[str, Any]:
        """Current effective settings + which secrets are set (never the values)."""
        from autobot.secrets import has_secret

        data: dict[str, Any] = Settings.load(path).to_dict()
        data["_secrets"] = {name: has_secret(name) for name in _SECRET_NAMES}
        return data

    async def post_settings(request: Request) -> dict[str, Any]:
        """Persist the given setting keys (sparse merge onto the file)."""
        payload = await request.json()
        if not isinstance(payload, dict):
            return {"ok": False, "error": "expected a JSON object"}
        valid = setting_names()
        updates = {k: v for k, v in payload.items() if k in valid}
        merged = {**read_settings(path), **updates}
        write_settings(merged, path)
        if on_change:
            on_change()  # let the engine pick up the change live (next turn)
        return {"ok": True, "applied": sorted(updates)}

    async def get_models() -> dict[str, Any]:
        """Installed local Ollama models, for the Settings view's model picker."""
        return {"models": _installed_ollama_models(Settings.load(path).ollama_host)}

    async def get_setup() -> dict[str, Any]:
        """First-run status the orb uses to decide whether to show the setup wizard.

        ``needs_setup`` is true when there's no settings file yet (a fresh install);
        the rest lets the wizard pre-fill (provider, whether a key/voice/Ollama exist).
        """
        from pathlib import Path as _Path

        from autobot.secrets import has_secret

        settings = Settings.load(path)
        voice = _Path(settings.tts_voice).expanduser()
        return {
            "needs_setup": not _Path(path).expanduser().exists(),
            "provider": settings.llm_provider,
            "has_anthropic_key": has_secret("anthropic_api_key"),
            "ollama_models": _installed_ollama_models(settings.ollama_host),
            "voice_present": voice.exists(),
        }

    async def get_report() -> dict[str, str]:
        """Build a compact, redacted debug report the user can copy or save.

        Bounded by the in-memory breadcrumb buffer plus a tail of the log file, so
        it stays small regardless of how long the session ran. No secrets/PII.
        """
        from pathlib import Path as _Path

        from autobot.diagnostics import build_report

        settings = Settings.load(path)
        log_path = _Path(settings.log_dir).expanduser() / "autobot.log"
        return {"report": build_report(settings, log_path=log_path)}

    async def get_report_file() -> dict[str, str]:
        """Write the debug report to a file and return its path (for Reveal in Finder)."""
        from pathlib import Path as _Path

        from autobot.diagnostics import save_report

        settings = Settings.load(path)
        log_path = _Path(settings.log_dir).expanduser() / "autobot.log"
        return {"path": str(save_report(settings, log_path=log_path))}

    async def post_secret(request: Request) -> dict[str, Any]:
        """Store (or clear) an API key in the Keychain. Only known names allowed."""
        from autobot.secrets import delete_secret, set_secret

        payload = await request.json()
        name = payload.get("name") if isinstance(payload, dict) else None
        if name not in _SECRET_NAMES:
            return {"ok": False, "error": f"unknown secret; allowed: {list(_SECRET_NAMES)}"}
        value = str(payload.get("value", ""))
        ok = set_secret(name, value) if value else delete_secret(name)
        if ok and on_change:
            on_change()  # a new key takes effect on the next turn, no restart
        return {"ok": ok}

    async def post_confirm(request: Request) -> dict[str, Any]:
        """Deliver a clicked Yes/No answer for a pending confirmation to the engine."""
        payload = await request.json()
        if not isinstance(payload, dict) or "answer" not in payload:
            return {"ok": False, "error": "expected {answer: bool}"}
        if on_confirm_answer is not None:
            on_confirm_answer(bool(payload["answer"]))
        return {"ok": True}

    # Register routes explicitly (rather than via decorators) so the handlers
    # stay statically typed under mypy strict — FastAPI ships no type info here.
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_websocket_route("/ws", ws)
    app.add_api_route("/settings", get_settings, methods=["GET"])
    app.add_api_route("/settings", post_settings, methods=["POST"])
    app.add_api_route("/models", get_models, methods=["GET"])
    app.add_api_route("/setup", get_setup, methods=["GET"])
    app.add_api_route("/report", get_report, methods=["GET"])
    app.add_api_route("/report/file", get_report_file, methods=["GET"])
    app.add_api_route("/secret", post_secret, methods=["POST"])
    app.add_api_route("/confirm", post_confirm, methods=["POST"])
    return app


def run_daemon(
    bus: EventBus,
    host: str,
    port: int,
    on_change: Any | None = None,
    on_confirm_answer: Any | None = None,
) -> None:
    """Run the daemon server (blocking) on ``host:port``.

    Refuses to bind anything other than loopback — the daemon is local-only by
    design, and a non-loopback bind would expose the engine on the network.
    ``on_change`` is invoked after a settings/secret update so the engine can pick
    it up live; ``on_confirm_answer`` delivers a clicked Yes/No to the engine.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"daemon must bind loopback only, got host={host!r}")
    import uvicorn

    _log.info("serving host=%s port=%d", host, port)
    # lifespan="off": we use no startup/shutdown events, and leaving it on emits a
    # noisy CancelledError traceback on Ctrl-C. KeyboardInterrupt is swallowed for
    # a clean exit.
    app = create_app(bus, on_change=on_change, on_confirm_answer=on_confirm_answer)
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    print("\n[daemon] stopped.")


def daemon_settings(settings: Settings) -> tuple[str, int]:
    """Extract the daemon bind address from settings (small helper for callers)."""
    return settings.daemon_host, settings.daemon_port
