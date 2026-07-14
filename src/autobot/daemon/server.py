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
import json
import threading
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

# Imported at module top (not lazily) on purpose: with ``from __future__ import
# annotations`` the ``websocket: WebSocket`` hint is a *string*, and FastAPI
# resolves it against this module's globals via ``get_type_hints``. If ``WebSocket``
# were imported inside the function, that resolution fails and FastAPI mistakes
# the parameter for a required query field (closing every connection with 1008).
# This module is the daemon transport, so requiring FastAPI to import it is fine;
# nothing on the core/engine import path pulls this in. uvicorn stays lazy.
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from autobot.core.events import EventBus, StateEvent
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from autobot.config import Settings
    from autobot.mcp.manager import McpManager
    from autobot.mcp.provider import McpProvider

_log = get_logger("daemon")


def _sse_frames(events: Iterable[dict[str, Any]]) -> Iterator[str]:
    """Serialize coder events to SSE ``data:`` frames; a bad event degrades to an error frame."""
    for evt in events:
        try:
            payload = json.dumps(evt)
        except (TypeError, ValueError):
            payload = json.dumps({"status": "error", "reply": "unserializable event"})
        yield f"data: {payload}\n\n"


# Secrets the Settings view may store (Keychain account names). Anything else is
# rejected so the endpoint can't write arbitrary Keychain items.
_SECRET_NAMES = ("anthropic_api_key", "openai_api_key", "web_api_key")

# Valid values for the ``risk`` field on the /mcp/servers/{id}/tools/{tool} endpoint.
_VALID_MCP_RISKS = frozenset({"read_only", "write", "destructive"})


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
    mcp_provider: McpProvider | None = None,
    on_meeting: Any | None = None,
    on_list_sessions: Any | None = None,
    on_resume_session: Any | None = None,
    on_coder_turn: Any | None = None,
    on_coder_reply: Any | None = None,
    on_coder_undo: Any | None = None,
    on_coder_checkpoints: Any | None = None,
    on_usage: Any | None = None,
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
        mcp_provider: Optional MCP provider (wired by the daemon). The /mcp/* routes
            resolve the live manager through it, and /settings flips it on/off when
            ``allow_mcp`` changes — so MCP can be enabled at runtime with no restart.
        on_meeting: Optional callable ``(action: str, payload: dict) -> object``
            dispatching meeting actions (``start``/``stop``/``pause``/``resume``/
            ``status``/``list``/``last``/``reveal``) to the recorder. When ``None``,
            all /meeting/* routes return ``{"ok": False, "error": "meetings disabled"}``.
        on_list_sessions: Optional callback returning the stored agent sessions
            (id/cwd/model/mtime summaries); wired to the orchestrator's
            ``list_sessions``. When ``None``, ``GET /sessions`` returns ``[]``.
        on_resume_session: Optional callback (session_id: str -> bool) that resumes a
            stored agent session; wired to the orchestrator's ``resume_session``. When
            ``None``, ``POST /sessions/resume`` returns ``{"ok": False}``.
        on_coder_turn: Optional callback (text: str -> Iterator[dict]) that starts a
            coder plan→approve→act turn and streams its events; wired to the
            orchestrator's ``start_coder_stream``. When ``None``, ``POST /coder/turn``
            streams a single error status event.
        on_coder_reply: Optional callback (value: str, text: str -> Iterator[dict])
            that delivers the CLI's answer to a parked coder turn and streams the
            next phase's events; wired to the orchestrator's ``reply_coder_stream``.
            When ``None``, ``POST /coder/reply`` streams a single error status event.
        on_coder_undo: Optional callback (() -> tuple[bool, str]) that restores the
            most recent coder checkpoint; wired to the orchestrator's
            ``undo_coder``. When ``None``, ``POST /coder/undo`` returns
            ``{"ok": False, "message": "undo unavailable"}``.
        on_coder_checkpoints: Optional callback (() -> list[dict[str, str]]) that
            lists coder checkpoints newest-first; wired to the orchestrator's
            ``list_coder_checkpoints``. When ``None``, ``GET /coder/checkpoints``
            returns ``{"checkpoints": []}``.
        on_usage: Optional callback (() -> dict) returning the live session usage
            plus ledger rollups; wired to the orchestrator's ``coder_usage``. When
            ``None``, ``GET /coder/usage`` returns ``{}``.

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
        # Turn the MCP subsystem on/off at runtime when allow_mcp changes — no restart.
        # (set_enabled is idempotent; shutdown can block briefly, so run off the loop.)
        if mcp_provider is not None and "allow_mcp" in updates:
            await asyncio.to_thread(mcp_provider.set_enabled, bool(merged.get("allow_mcp")))
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
            "has_openai_key": has_secret("openai_api_key"),
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

    async def post_coder_turn(request: Request) -> Any:
        """Start a coder plan→approve→act turn; stream its events as SSE."""
        payload = await request.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not text or on_coder_turn is None:
            return StreamingResponse(
                _sse_frames(iter([{"status": "error", "reply": "no text / coder unavailable"}])),
                media_type="text/event-stream",
            )
        return StreamingResponse(
            _sse_frames(on_coder_turn(str(text))), media_type="text/event-stream"
        )

    async def post_coder_reply(request: Request) -> Any:
        """Deliver the CLI's answer to a parked coder turn; stream the next phase as SSE."""
        payload = await request.json()
        value = payload.get("value") if isinstance(payload, dict) else None
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        if value is None or on_coder_reply is None:
            return StreamingResponse(
                _sse_frames(iter([{"status": "error", "reply": "no value / coder unavailable"}])),
                media_type="text/event-stream",
            )
        return StreamingResponse(
            _sse_frames(on_coder_reply(str(value), str(text))), media_type="text/event-stream"
        )

    async def post_coder_undo() -> dict[str, Any]:
        """Restore the most recent coder checkpoint. Runs off the loop (takes the driver lock)."""
        if on_coder_undo is None:
            return {"ok": False, "message": "undo unavailable"}
        ok, message = await asyncio.to_thread(on_coder_undo)
        return {"ok": bool(ok), "message": message}

    async def get_coder_checkpoints() -> dict[str, Any]:
        """List coder checkpoints (newest first)."""
        if on_coder_checkpoints is None:
            return {"checkpoints": []}
        rows = await asyncio.to_thread(on_coder_checkpoints)
        return {"checkpoints": rows if isinstance(rows, list) else []}

    async def get_coder_usage() -> dict[str, Any]:
        """Live session usage + ledger rollups. Empty dict when unwired."""
        if on_usage is None:
            return {}
        result = await asyncio.to_thread(on_usage)
        return result if isinstance(result, dict) else {}

    async def post_new_session() -> dict[str, Any]:
        """Start a fresh chat session — discard the engine's conversation history.

        Runs off the event loop: the reset takes the engine's turn lock, so it waits
        for any in-flight turn before clearing (and never blocks the WebSocket).
        """
        if on_new_session is not None:
            await asyncio.to_thread(on_new_session)
        return {"ok": True}

    async def get_sessions() -> list[dict[str, Any]]:
        """List stored agent sessions (id/cwd/model/mtime), most recent first."""
        if on_list_sessions is None:
            return []
        result = await asyncio.to_thread(on_list_sessions)
        return result if isinstance(result, list) else []

    async def post_sessions_resume(request: Request) -> dict[str, Any]:
        """Resume a stored agent session (``{id}``) — replaces the live conversation."""
        payload = await request.json()
        session_id = payload.get("id") if isinstance(payload, dict) else None
        if not session_id or on_resume_session is None:
            return {"ok": False}
        ok = await asyncio.to_thread(on_resume_session, str(session_id))
        return {"ok": bool(ok)}

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

    # --------------------------------------------------------------- Meeting
    # All /meeting/* handlers check on_meeting is not None and return a graceful
    # error when meetings are disabled (allow_meetings=False). Blocking recorder
    # calls are run via asyncio.to_thread so the event loop stays responsive.

    _meeting_disabled: dict[str, object] = {"ok": False, "error": "meetings disabled"}

    async def post_meeting(action: str, request: Request) -> dict[str, Any]:
        """Dispatch a meeting write action (start/stop/pause/resume) off the loop."""
        if on_meeting is None:
            return _meeting_disabled
        payload: object = {}
        if action in ("start", "reveal"):
            payload = await request.json()
        reply = await asyncio.to_thread(
            on_meeting, action, payload if isinstance(payload, dict) else {}
        )
        return {"ok": True, "reply": reply}

    async def get_meeting_status() -> dict[str, Any]:
        """Return the recorder's current status snapshot."""
        if on_meeting is None:
            return {"status": {"active": False}}
        status = await asyncio.to_thread(on_meeting, "status", {})
        return {"status": status}

    async def get_meeting_list() -> dict[str, Any]:
        """Return a list of recent meeting summaries."""
        if on_meeting is None:
            return {"meetings": []}
        meetings = await asyncio.to_thread(on_meeting, "list", {})
        return {"meetings": meetings}

    async def get_meeting_last() -> dict[str, Any]:
        """Return the most recent finished meeting's minutes, or ``{"ok": False}``.

        The front-end calls this when a ``meeting`` event with ``state:"done"``
        arrives, to render a minutes card.  Returns ``{"ok": True, ...}`` with the
        payload from ``recorder.last_minutes()`` when a finished meeting exists,
        or ``{"ok": False}`` when meetings are disabled or no finished meeting is
        found.
        """
        if on_meeting is None:
            return {"ok": False}
        payload = await asyncio.to_thread(on_meeting, "last", {})
        if isinstance(payload, dict):
            return {"ok": True, **payload}
        return {"ok": False}

    # ------------------------------------------------------------------ MCP
    # All /mcp/* handlers check mcp is not None and return a graceful error
    # when MCP is disabled (allow_mcp=False). Blocking manager calls are run
    # via asyncio.to_thread so the event loop stays responsive.

    _mcp_disabled: dict[str, object] = {"ok": False, "error": "mcp disabled"}

    def _mcp() -> McpManager | None:
        """Resolve the live MCP manager, or None when disabled.

        Per-request resolution lets a runtime ``allow_mcp`` toggle apply with no restart.
        """
        return mcp_provider.manager if mcp_provider is not None else None

    async def get_mcp_servers() -> dict[str, Any]:
        """List all configured MCP servers with status + auth metadata."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        rows = await asyncio.to_thread(mcp.status)
        # status() rows already carry auth_type; enrich each with secret_present
        # (a Keychain lookup) via the public method — no reaching into mcp internals.
        enriched: list[dict[str, Any]] = [
            {
                **row,
                "secret_present": await asyncio.to_thread(mcp.secret_present, str(row["server"])),
            }
            for row in rows
        ]
        return {"ok": True, "servers": enriched}

    async def post_mcp_servers(request: Request) -> dict[str, Any]:
        """Add or update an MCP server (validated via _coerce_server)."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        body = await request.json()
        if not isinstance(body, dict):
            return {"ok": False, "error": "expected a JSON object"}
        return await asyncio.to_thread(mcp.add_or_update_server, body)

    async def delete_mcp_server(server_id: str) -> dict[str, Any]:
        """Remove a configured MCP server."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        removed = await asyncio.to_thread(mcp.remove_server, server_id)
        if not removed:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        return {"ok": True}

    async def post_mcp_enable(server_id: str) -> dict[str, Any]:
        """Enable an MCP server (persist + connect)."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        ok = await asyncio.to_thread(mcp.set_enabled, server_id, True)
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_disable(server_id: str) -> dict[str, Any]:
        """Disable an MCP server (persist + disconnect)."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        ok = await asyncio.to_thread(mcp.set_enabled, server_id, False)
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_connect(server_id: str) -> dict[str, Any]:
        """Connect to an MCP server (start worker if not running)."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        await asyncio.to_thread(mcp.connect, server_id)
        return {"ok": True}

    async def post_mcp_test(server_id: str) -> dict[str, Any]:
        """Connect to an MCP server and return its current status row."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        await asyncio.to_thread(mcp.connect, server_id)
        rows = await asyncio.to_thread(mcp.status)
        match = next((r for r in rows if r["server"] == server_id), None)
        if match is None:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        return {"ok": True, "server": match}

    async def get_mcp_tools(server_id: str) -> dict[str, Any]:
        """Return the cached all-tools list for a server."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        tools = await asyncio.to_thread(mcp.tools_for, server_id)
        return {"ok": True, "tools": tools}

    async def post_mcp_tool_override(server_id: str, tool: str, request: Request) -> dict[str, Any]:
        """Adjust a tool's risk classification or enable/disable it."""
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        body = await request.json()
        risk: str | None = body.get("risk") if isinstance(body, dict) else None
        enabled: bool | None = body.get("enabled") if isinstance(body, dict) else None
        if risk is not None and risk not in _VALID_MCP_RISKS:
            return {"ok": False, "error": "invalid risk; must be read_only|write|destructive"}
        ok = await asyncio.to_thread(
            mcp.set_tool_override, server_id, tool, risk=risk, enabled=enabled
        )
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_auth_start(server_id: str) -> dict[str, Any]:
        """Initiate the OAuth 2.1 flow for an oauth HTTP server.

        Disconnects any existing worker and reconnects, which triggers the
        browser-open + loopback callback inside the worker's event loop. Stage
        events (``mcp_oauth``) are published via the WS event bus. Non-blocking.
        """
        if (mcp := _mcp()) is None:
            return _mcp_disabled
        return await asyncio.to_thread(mcp.start_oauth, server_id)

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
    app.add_api_route("/coder/turn", post_coder_turn, methods=["POST"])
    app.add_api_route("/coder/reply", post_coder_reply, methods=["POST"])
    app.add_api_route("/coder/undo", post_coder_undo, methods=["POST"])
    app.add_api_route("/coder/checkpoints", get_coder_checkpoints, methods=["GET"])
    app.add_api_route("/coder/usage", get_coder_usage, methods=["GET"])
    app.add_api_route("/session/new", post_new_session, methods=["POST"])
    app.add_api_route("/sessions", get_sessions, methods=["GET"])
    app.add_api_route("/sessions/resume", post_sessions_resume, methods=["POST"])
    app.add_api_route("/voice/status", get_voice_status, methods=["GET"])
    app.add_api_route("/voice/download", post_voice_download, methods=["POST"])

    # Named, Request-typed handlers (NOT `lambda r: ...` — an untyped lambda param
    # makes FastAPI treat `r` as a required query param and 422 every POST).
    async def _post_meeting_start(request: Request) -> dict[str, Any]:
        return await post_meeting("start", request)

    async def _post_meeting_stop(request: Request) -> dict[str, Any]:
        return await post_meeting("stop", request)

    async def _post_meeting_pause(request: Request) -> dict[str, Any]:
        return await post_meeting("pause", request)

    async def _post_meeting_resume(request: Request) -> dict[str, Any]:
        return await post_meeting("resume", request)

    async def _post_meeting_reveal(request: Request) -> dict[str, Any]:
        return await post_meeting("reveal", request)

    app.add_api_route("/meeting/start", _post_meeting_start, methods=["POST"])
    app.add_api_route("/meeting/stop", _post_meeting_stop, methods=["POST"])
    app.add_api_route("/meeting/pause", _post_meeting_pause, methods=["POST"])
    app.add_api_route("/meeting/resume", _post_meeting_resume, methods=["POST"])
    app.add_api_route("/meeting/reveal", _post_meeting_reveal, methods=["POST"])
    app.add_api_route("/meeting/status", get_meeting_status, methods=["GET"])
    app.add_api_route("/meeting/list", get_meeting_list, methods=["GET"])
    app.add_api_route("/meeting/last", get_meeting_last, methods=["GET"])
    app.add_api_route("/mcp/servers", get_mcp_servers, methods=["GET"])
    app.add_api_route("/mcp/servers", post_mcp_servers, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}", delete_mcp_server, methods=["DELETE"])
    app.add_api_route("/mcp/servers/{server_id}/enable", post_mcp_enable, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/disable", post_mcp_disable, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/connect", post_mcp_connect, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/test", post_mcp_test, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/tools", get_mcp_tools, methods=["GET"])
    app.add_api_route(
        "/mcp/servers/{server_id}/tools/{tool}", post_mcp_tool_override, methods=["POST"]
    )
    app.add_api_route("/mcp/servers/{server_id}/auth/start", post_mcp_auth_start, methods=["POST"])
    return app


def _install_idle_shutdown(app: Any, idle_timeout: float) -> None:
    """Self-terminate the daemon after ``idle_timeout`` seconds with no HTTP requests.

    A per-workspace coder daemon should not linger forever: a middleware stamps the last
    request time and a background thread SIGTERMs the process (uvicorn shuts down cleanly)
    once it's been idle too long, so daemons for projects you've stopped using go away.
    """
    import os
    import signal
    import threading
    import time
    from collections.abc import Awaitable, Callable

    last = {"t": time.monotonic()}

    async def _stamp(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        last["t"] = time.monotonic()
        return await call_next(request)

    # Register via a call rather than decorating the def: ``app`` is Any, so decorating the
    # definition would make _stamp itself untyped (a mypy-strict error). A plain call keeps
    # _stamp typed and discards the Any return.
    app.middleware("http")(_stamp)

    def _reaper() -> None:
        interval = max(5.0, min(60.0, idle_timeout / 4))
        while True:
            time.sleep(interval)
            if time.monotonic() - last["t"] > idle_timeout:
                _log.info("coder daemon idle for %.0fs — shutting down", idle_timeout)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=_reaper, name="idle-reaper", daemon=True).start()


def run_daemon(
    bus: EventBus,
    host: str,
    port: int,
    on_change: Any | None = None,
    on_confirm_answer: Any | None = None,
    on_chat: Any | None = None,
    on_new_session: Any | None = None,
    on_action: Any | None = None,
    mcp_provider: McpProvider | None = None,
    on_meeting: Any | None = None,
    on_list_sessions: Any | None = None,
    on_resume_session: Any | None = None,
    on_coder_turn: Any | None = None,
    on_coder_reply: Any | None = None,
    on_coder_undo: Any | None = None,
    on_coder_checkpoints: Any | None = None,
    on_usage: Any | None = None,
    idle_timeout: float | None = None,
) -> None:
    """Run the daemon server (blocking) on ``host:port``.

    Refuses to bind anything other than loopback — the daemon is local-only by
    design, and a non-loopback bind would expose the engine on the network.
    ``on_change`` is invoked after a settings/secret update so the engine can pick
    it up live; ``on_confirm_answer`` delivers a clicked Yes/No to the engine.
    ``on_meeting`` dispatches /meeting/* HTTP actions to the MeetingRecorder.
    ``on_list_sessions``/``on_resume_session`` back the ``/sessions`` endpoints.
    ``on_coder_turn``/``on_coder_reply`` back the ``/coder/turn``/``/coder/reply``
    endpoints. ``on_coder_undo``/``on_coder_checkpoints`` back the
    ``/coder/undo``/``/coder/checkpoints`` endpoints.
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
        mcp_provider=mcp_provider,
        on_meeting=on_meeting,
        on_list_sessions=on_list_sessions,
        on_resume_session=on_resume_session,
        on_coder_turn=on_coder_turn,
        on_coder_reply=on_coder_reply,
        on_coder_undo=on_coder_undo,
        on_coder_checkpoints=on_coder_checkpoints,
        on_usage=on_usage,
    )
    if idle_timeout and idle_timeout > 0:
        _install_idle_shutdown(app, idle_timeout)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    finally:
        if mcp_provider is not None:
            # Suppress a stray Ctrl+C during MCP teardown so the daemon still exits
            # cleanly (reaching the "stopped" line) instead of dumping a traceback.
            # The manager already tolerates KeyboardInterrupt internally; this is the
            # last-resort guard for any wait it doesn't cover.
            with contextlib.suppress(KeyboardInterrupt):
                mcp_provider.shutdown()
    print("\n[daemon] stopped.")


def daemon_settings(settings: Settings) -> tuple[str, int]:
    """Extract the daemon bind address from settings (small helper for callers)."""
    return settings.daemon_host, settings.daemon_port
