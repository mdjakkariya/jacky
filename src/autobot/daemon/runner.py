"""Wire the engine and the daemon together, then serve.

The orchestrator runs the interaction loop on a background thread and publishes
state transitions to an :class:`~autobot.core.events.EventBus`; the daemon serves
that bus over a localhost WebSocket on the main thread. A ``--demo`` path cycles
the states with no mic or model, so the orb UI can be developed against a live
socket before the full engine is wired in.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

from autobot.config import Settings
from autobot.core.events import AmplitudeSink, EventBus, OrbState, orb_state_for
from autobot.core.types import State
from autobot.daemon.server import run_daemon
from autobot.logging_setup import get_logger
from autobot.orchestrator.state_machine import StateListener, _print_transition

_log = get_logger("daemon")


def make_state_listener(bus: EventBus, is_chat: Callable[[], bool] | None = None) -> StateListener:
    """Adapt the engine's transition callback to publish onto the bus.

    Keeps the existing console line (so terminal runs are unchanged) and also
    forwards the mapped :class:`OrbState` to every connected UI client — except in
    chat mode, where the orb is the *voice* UI and must stay hidden: a typed turn's
    THINKING/TALKING transitions would otherwise pop the orb up over the chat drawer.
    """
    from autobot.diagnostics import get_buffer

    buffer = get_buffer()

    def listener(old: State, new: State) -> None:
        _print_transition(old, new)
        buffer.add_state(old.value, new.value)  # compact sequence trace for reports
        if is_chat is not None and is_chat():
            return  # chat mode: don't drive the voice orb
        bus.publish_state(orb_state_for(new))

    return listener


def make_amplitude_sink(bus: EventBus) -> AmplitudeSink:
    """Publish loudness only while the orb is *listening* or *talking*.

    The mic emits a frame ~30 times a second the whole time the engine is capturing
    (which, hands-free, is almost always) — forwarding it unconditionally would be
    constant noise on the wire. We gate on the orb state: amplitude flows while the
    user is actually speaking (LISTENING, set by the voice sink on VAD) and while
    Jack is speaking (TALKING), and stays quiet at rest.
    """

    def sink(level: float) -> None:
        if bus.last_state in (OrbState.LISTENING, OrbState.TALKING):
            bus.publish_amplitude(level)

    return sink


def make_voice_sink(bus: EventBus) -> Callable[[bool], None]:
    """Reflect real voice activity on the orb: LISTENING while the user speaks.

    Driven by the recorder's VAD (not the coarse engine state), so the orb only
    animates "listening" when there's actual speech and rests when the user is
    silent. When speech ends, the engine's own state transitions take the orb on
    to thinking/talking; we drop back to IDLE here as a clean default.
    """

    def sink(active: bool) -> None:
        bus.publish_state(OrbState.LISTENING if active else OrbState.IDLE)

    return sink


def serve(settings: Settings | None = None) -> None:
    """Run the real engine behind the daemon (blocking).

    Building the orchestrator loads the STT model, the audit log, the sandbox,
    and connects to Ollama — same as a normal run, just with a UI-facing socket.
    """
    settings = settings or Settings.load()
    from autobot.app import build
    from autobot.daemon.watchdog import start_parent_watchdog
    from autobot.tools.confirm import ConfirmInbox

    # Die if the orb (our launching parent) goes away, so we never linger on :8765.
    start_parent_watchdog()

    bus = EventBus()
    # Bridge clicked Yes/No on a confirmation card (daemon thread) to the engine.
    inbox = ConfirmInbox()
    # The state listener needs to know the engine's mode to keep the orb quiet in
    # chat — but the orchestrator doesn't exist yet, so consult it lazily.
    holder: dict[str, object] = {}

    def _is_chat() -> bool:
        orch = holder.get("orch")
        return bool(orch is not None and orch.in_chat_mode())  # type: ignore[attr-defined]

    def publish_context(info: dict[str, object]) -> None:
        bus.publish_context(info, dev=settings.show_debug)

    def publish_choices(title: str, items: list[dict[str, Any]]) -> None:
        # Tag the card with the live mode, like confirmations. The chat drawer renders
        # only chat-mode choices, so a search run *in voice mode* (acted on by speech)
        # no longer leaves a stray card lingering in an otherwise-empty chat drawer.
        bus.publish_choices(title, items, chat=_is_chat())

    def publish_step(index: int, tool: str, label: str, status: str) -> None:
        bus.publish_step(index, tool, label, status)

    def publish_workspace(path: str, name: str) -> None:
        bus.publish_workspace(path, name)

    orchestrator = build(
        settings,
        on_state=make_state_listener(bus, is_chat=_is_chat),
        amplitude_sink=make_amplitude_sink(bus),
        on_visibility=bus.publish_visibility,
        on_voice=make_voice_sink(bus),
        on_confirm=bus.publish_confirm,
        on_confirm_clear=bus.publish_confirm_clear,
        poll_click=inbox.take,
        on_context=publish_context,
        on_choices=publish_choices,
        on_step=publish_step,
        on_workspace=publish_workspace,
        on_mcp_event=bus.publish_mcp,
        on_meeting_event=bus.publish_meeting,
    )
    holder["orch"] = orchestrator

    # Build the meeting dispatcher if the recorder was wired. HTTP WRITE actions
    # (start/stop/pause/resume) bypass the audit gate and call the recorder directly.
    # This is intentional: these are UI-initiated controls (not LLM-driven tool calls),
    # and the recorder itself logs state transitions. The voice/chat tool path (Task 15)
    # remains the gated/audited path for LLM-originated meeting actions.
    _recorder = getattr(orchestrator, "meeting_recorder", None)

    def _on_meeting(action: str, payload: dict[str, Any]) -> object:
        """Dispatch a /meeting/* HTTP action to the recorder."""
        if _recorder is None:
            return {"error": "meetings disabled"}
        if action == "start":
            title = str(payload.get("title", "")) if isinstance(payload, dict) else ""
            return _recorder.start(title)
        if action == "stop":
            return _recorder.stop()
        if action == "pause":
            return _recorder.pause()
        if action == "resume":
            return _recorder.resume()
        if action == "status":
            return _recorder.status()
        if action == "list":
            return _recorder.list_recent()
        if action == "last":
            return _recorder.last_minutes()
        if action == "reveal":
            meeting_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
            return _recorder.reveal(meeting_id)
        return {"error": f"unknown action: {action!r}"}

    on_meeting = _on_meeting if _recorder is not None else None

    thread = threading.Thread(target=orchestrator.run, name="engine", daemon=True)
    thread.start()
    print(f"[daemon] serving on ws://{settings.daemon_host}:{settings.daemon_port}/ws")
    # Coder profile: the driver owns turn concurrency with its OWN lock over the shared
    # gate/confirmer/session, which is not safe to interleave with the assistant/drawer
    # turn-mutating endpoints (they take the orchestrator's own _turn_lock and could
    # route a /chat confirmation into a parked coder turn's channel). So the coder
    # profile disables `on_chat`/`on_action` — /coder/turn and /coder/reply are the only
    # turn entry point. `on_new_session`/`on_resume_session` stay wired for both profiles:
    # in the coder profile they route through the driver's own lock (`new_coder_session`/
    # `resume_coder_session`), so they're safe to interleave with a parked coder turn.
    # `on_list_sessions` stays wired (read-only; it's the `jack` readiness probe hitting
    # GET /sessions) as does `on_change`/`on_confirm_answer`/`mcp_provider`/`on_meeting`.
    coder = settings.profile == "coder"
    # Live-apply settings/key changes from the Settings view (next turn, no restart).
    run_daemon(
        bus,
        settings.daemon_host,
        settings.daemon_port,
        on_change=orchestrator.mark_settings_changed,
        on_confirm_answer=inbox.submit,
        on_chat=None if coder else orchestrator.run_text_turn,
        on_new_session=orchestrator.new_coder_session if coder else orchestrator.new_chat_session,
        on_action=None if coder else orchestrator.run_tool,
        mcp_provider=orchestrator.mcp_provider,
        on_meeting=on_meeting,
        on_list_sessions=orchestrator.list_sessions,
        on_resume_session=(
            orchestrator.resume_coder_session if coder else orchestrator.resume_session
        ),
        on_coder_turn=orchestrator.start_coder_stream,
        on_coder_reply=orchestrator.reply_coder_stream,
        on_coder_undo=orchestrator.undo_coder,
        on_coder_checkpoints=orchestrator.list_coder_checkpoints,
    )


def serve_demo(settings: Settings | None = None) -> None:
    """Serve a scripted state cycle with no engine — for developing the UI.

    Walks idle → listening → thinking → talking on a timer and emits a synthetic
    amplitude envelope, so the orb can be wired to a live socket without a mic,
    a model, or Ollama running.
    """
    settings = settings or Settings.load()
    from autobot.daemon.watchdog import start_parent_watchdog

    start_parent_watchdog()
    bus = EventBus()

    def cycle() -> None:
        order = [OrbState.IDLE, OrbState.LISTENING, OrbState.THINKING, OrbState.TALKING]
        i = 0
        start = time.perf_counter()
        while True:
            state = order[i % len(order)]
            bus.publish_state(state)
            _log.info("demo state=%s", state.value)
            # Emit a synthetic amplitude envelope for the reactive states.
            hold = 3.0
            step = 0.05
            elapsed = 0.0
            while elapsed < hold:
                if state in {OrbState.LISTENING, OrbState.TALKING}:
                    t = time.perf_counter() - start
                    amp = abs(math.sin(t * 3.0)) * 0.6 + abs(math.sin(t * 7.3)) * 0.4
                    bus.publish_amplitude(min(1.0, amp))
                time.sleep(step)
                elapsed += step
            i += 1

    threading.Thread(target=cycle, name="demo-cycle", daemon=True).start()
    print(f"[daemon] DEMO serving on ws://{settings.daemon_host}:{settings.daemon_port}/ws")
    run_daemon(bus, settings.daemon_host, settings.daemon_port)
