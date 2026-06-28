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
import threading
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


def _is_allowed_secret(name: str) -> bool:
    """Whether ``name`` is a permitted Keychain account the Settings view may write.

    Accepts the hard-coded core secrets (API keys for existing providers) AND any
    account under the ``mcp.`` namespace (e.g. ``mcp.slack.token``, ``mcp.gh.oauth``).
    Rejects bare ``"mcp."`` (no sub-key) and arbitrary names.
    """
    if name in _SECRET_NAMES:
        return True
    # e.g. "mcp.slack.token" → prefix "mcp." + at least one char after the dot
    return name.startswith("mcp.") and len(name) > len("mcp.")


# Guards the on-demand voice-model download so two clicks can't run it twice.
_voice_download_lock = threading.Lock()


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
    on_chat: Any | None = None,
    on_new_session: Any | None = None,
    on_action: Any | None = None,
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
        on_chat: Optional callback (str -> str) handling one typed turn and returning
            the reply; wired to the orchestrator's ``run_text_turn``.
        on_new_session: Optional callback invoked to discard the conversation and
            start fresh; wired to the orchestrator's ``new_chat_session``.
        on_action: Optional callback (tool: str, args: dict -> str) that runs one tool
            through the permission gate for a clicked action card; wired to the
            orchestrator's ``run_tool``.

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
            # Replay the active folder so the chat drawer shows the current cwd immediately.
            if bus.last_workspace is not None:
                await websocket.send_json(bus.last_workspace)
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

    async def get_report_concise() -> dict[str, str]:
        """Concise debug report for local debugging (the dev copy button).

        Distinct from ``/report`` (the full GitHub-issue report): bounded to the
        recent window and without the raw log dump, so it's small enough to paste
        into a debug chat without exhausting it.
        """
        from autobot.diagnostics import build_dev_report

        return {"report": build_dev_report(Settings.load(path))}

    async def get_report_file() -> dict[str, str]:
        """Write the debug report to a file and return its path (for Reveal in Finder)."""
        from pathlib import Path as _Path

        from autobot.diagnostics import save_report

        settings = Settings.load(path)
        log_path = _Path(settings.log_dir).expanduser() / "autobot.log"
        return {"path": str(save_report(settings, log_path=log_path))}

    async def get_permissions() -> dict[str, Any]:
        """Current macOS permission statuses for the Settings view."""
        from autobot import permissions

        return {"permissions": permissions.snapshot()}

    async def post_permission_open(request: Request) -> dict[str, Any]:
        """Open the System Settings privacy pane for a permission key."""
        from autobot import permissions

        payload = await request.json()
        key = payload.get("key") if isinstance(payload, dict) else None
        ok = permissions.open_pane(str(key)) if key else False
        return {"ok": ok}

    async def post_secret(request: Request) -> dict[str, Any]:
        """Store (or clear) an API key in the Keychain. Only known names allowed."""
        from autobot.secrets import delete_secret, set_secret

        payload = await request.json()
        name = payload.get("name") if isinstance(payload, dict) else None
        name_str = str(name) if name is not None else ""
        if not _is_allowed_secret(name_str):
            return {
                "ok": False,
                "error": (
                    f"unknown secret; allowed: {list(_SECRET_NAMES)} or any 'mcp.<id>.*' name"
                ),
            }
        value = str(payload.get("value", ""))
        ok = set_secret(name_str, value) if value else delete_secret(name_str)
        if ok and on_change:
            on_change()  # a new key takes effect on the next turn, no restart
        return {"ok": ok}

    async def post_chat(request: Request) -> dict[str, Any]:
        """Handle one typed turn (chat mode): run it on a worker thread, return reply."""
        payload = await request.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not text or on_chat is None:
            return {"ok": False, "reply": "", "error": "no text / chat unavailable"}
        # run_text_turn is blocking (LLM + tools); keep it off the event loop so the
        # WebSocket and other clients stay responsive while Jack thinks.
        reply = await asyncio.to_thread(on_chat, str(text))
        return {"ok": True, "reply": reply}

    async def post_new_session() -> dict[str, Any]:
        """Start a fresh chat session — discard the engine's conversation history.

        Runs off the event loop: the reset takes the engine's turn lock, so it waits
        for any in-flight turn before clearing (and never blocks the WebSocket).
        """
        if on_new_session is not None:
            await asyncio.to_thread(on_new_session)
        return {"ok": True}

    async def get_voice_status() -> dict[str, Any]:
        """Which voice models are present, and whether voice can be enabled."""
        from autobot import voice_setup

        return voice_setup.status(Settings.load(path))

    async def post_voice_download() -> dict[str, Any]:
        """Start downloading the missing voice models; progress streams over the WS.

        Runs on a background thread (network + disk) and publishes ``voice_download``
        events with the overall fraction; the Settings view renders the bar and, on
        ``done``, can enable voice. Refuses to start a second concurrent download.
        """
        from autobot import voice_setup

        settings = Settings.load(path)
        if not _voice_download_lock.acquire(blocking=False):
            return {"ok": False, "error": "a download is already in progress"}

        def run() -> None:
            try:
                voice_setup.download_missing(settings, bus.publish_voice_download)
                bus.publish_voice_download(1.0, "Ready", done=True)
            except Exception as exc:  # surface a short message; never crash the daemon
                _log.warning("voice download failed: %s", exc)
                bus.publish_voice_download(0.0, "Download failed", done=True, error=str(exc))
            finally:
                _voice_download_lock.release()

        threading.Thread(target=run, name="voice-download", daemon=True).start()
        return {"ok": True, "started": True}

    async def get_workspace() -> dict[str, Any]:
        """Report the active folder (cwd) + granted folders (for the chat folder modal)."""
        from autobot.tools.access import active_policy

        pol = active_policy()
        if pol is None:
            return {"path": "", "name": "", "grants": []}
        grants = [{"path": g.path, "mode": g.mode.name.lower()} for g in pol.grants()]
        return {"path": str(pol.cwd), "name": pol.cwd.name, "grants": grants}

    async def post_workspace(request: Request) -> dict[str, Any]:
        """Set the active folder (``{path}``) through the gate (grant card applies)."""
        payload = await request.json()
        if not isinstance(payload, dict) or "path" not in payload or on_action is None:
            return {"ok": False, "error": "expected {path} / action unavailable"}
        result = await asyncio.to_thread(
            on_action, "set_working_directory", {"path": str(payload["path"])}
        )
        return {"ok": True, "result": result}

    async def get_access() -> dict[str, Any]:
        """List the folders the user has granted Jack access to (for Settings)."""
        from autobot.tools.access import active_policy

        pol = active_policy()
        grants = (
            [{"path": g.path, "mode": g.mode.name.lower()} for g in pol.grants()] if pol else []
        )
        return {"grants": grants}

    async def post_access_grant(request: Request) -> dict[str, Any]:
        """Grant access to a folder (``{path, write}``) — the Settings 'Add folder'."""
        from autobot.tools.access import AccessDeniedError, active_policy

        payload = await request.json()
        pol = active_policy()
        if pol is None or not isinstance(payload, dict) or "path" not in payload:
            return {"ok": False, "error": "no path / access unavailable"}
        try:
            g = pol.grant(str(payload["path"]), write=bool(payload.get("write")))
        except AccessDeniedError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "grant": {"path": g.path, "mode": g.mode.name.lower()}}

    async def post_access_revoke(request: Request) -> dict[str, Any]:
        """Revoke a folder grant (``{path}``)."""
        from autobot.tools.access import active_policy

        payload = await request.json()
        pol = active_policy()
        if pol is None or not isinstance(payload, dict) or "path" not in payload:
            return {"ok": False, "error": "no path / access unavailable"}
        return {"ok": True, "removed": pol.revoke(str(payload["path"]))}

    async def post_action(request: Request) -> dict[str, Any]:
        """Run a UI-initiated action (a clicked action card) through the engine's gate.

        Generic: the body is ``{tool, args}`` naming a registered tool; it runs through
        the same permission gate the model uses (no LLM call). The action's tool/args
        are produced by the engine itself (a tool's choices), not free user input.
        """
        payload = await request.json()
        if not isinstance(payload, dict) or "tool" not in payload:
            return {"ok": False, "error": "expected {tool, args}"}
        tool = str(payload["tool"])
        args = payload.get("args") or {}
        if not isinstance(args, dict) or on_action is None:
            return {"ok": False, "error": "bad args / action unavailable"}
        result = await asyncio.to_thread(on_action, tool, args)
        return {"ok": True, "result": result}

    async def post_confirm(request: Request) -> dict[str, Any]:
        """Deliver a clicked answer for a pending confirmation/choice to the engine.

        Accepts ``{value: str}`` (e.g. "yes"/"no"/"read"/"write") or, for older
        clients, ``{answer: bool}`` which maps to "yes"/"no".
        """
        payload = await request.json()
        value = payload.get("value") if isinstance(payload, dict) else None
        if value is None and isinstance(payload, dict) and "answer" in payload:
            value = "yes" if payload["answer"] else "no"
        if value is None:
            return {"ok": False, "error": "expected {value} or {answer}"}
        if on_confirm_answer is not None:
            on_confirm_answer(str(value))
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
    app.add_api_route("/report/concise", get_report_concise, methods=["GET"])
    app.add_api_route("/report/file", get_report_file, methods=["GET"])
    app.add_api_route("/permissions", get_permissions, methods=["GET"])
    app.add_api_route("/permissions/open", post_permission_open, methods=["POST"])
    app.add_api_route("/secret", post_secret, methods=["POST"])
    app.add_api_route("/confirm", post_confirm, methods=["POST"])
    app.add_api_route("/action", post_action, methods=["POST"])
    app.add_api_route("/workspace", get_workspace, methods=["GET"])
    app.add_api_route("/workspace", post_workspace, methods=["POST"])
    app.add_api_route("/access", get_access, methods=["GET"])
    app.add_api_route("/access/grant", post_access_grant, methods=["POST"])
    app.add_api_route("/access/revoke", post_access_revoke, methods=["POST"])
    app.add_api_route("/chat", post_chat, methods=["POST"])
    app.add_api_route("/session/new", post_new_session, methods=["POST"])
    app.add_api_route("/voice/status", get_voice_status, methods=["GET"])
    app.add_api_route("/voice/download", post_voice_download, methods=["POST"])
    return app


def run_daemon(
    bus: EventBus,
    host: str,
    port: int,
    on_change: Any | None = None,
    on_confirm_answer: Any | None = None,
    on_chat: Any | None = None,
    on_new_session: Any | None = None,
    on_action: Any | None = None,
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
    app = create_app(
        bus,
        on_change=on_change,
        on_confirm_answer=on_confirm_answer,
        on_chat=on_chat,
        on_new_session=on_new_session,
        on_action=on_action,
    )
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    print("\n[daemon] stopped.")


def daemon_settings(settings: Settings) -> tuple[str, int]:
    """Extract the daemon bind address from settings (small helper for callers)."""
    return settings.daemon_host, settings.daemon_port
