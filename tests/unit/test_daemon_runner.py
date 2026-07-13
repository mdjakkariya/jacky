"""Tests for the daemon runner's state/amplitude adapters and turn-entry-point wiring.

Skipped when the optional ``daemon`` extra is absent (runner imports the server).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

from autobot.config import Settings
from autobot.core.events import EventBus, OrbState
from autobot.core.types import State
from autobot.daemon.runner import make_amplitude_sink, make_state_listener, make_voice_sink


def test_state_listener_publishes_mapped_orb_state() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    listener = make_state_listener(bus)

    listener(State.IDLE, State.PLANNING)

    assert seen == [{"type": "state", "value": "thinking"}]
    assert bus.last_state is OrbState.THINKING


def test_state_listener_stays_quiet_in_chat_mode() -> None:
    # The orb is the voice UI; in chat mode a typed turn's THINKING/TALKING must not
    # be published, or the orb pops up over the chat drawer.
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    chat = {"on": True}
    listener = make_state_listener(bus, is_chat=lambda: chat["on"])

    listener(State.IDLE, State.PLANNING)  # chat turn -> suppressed
    assert seen == []

    chat["on"] = False
    listener(State.IDLE, State.PLANNING)  # voice turn -> published
    assert seen == [{"type": "state", "value": "thinking"}]


def test_amplitude_sink_emits_while_listening_or_talking() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    sink = make_amplitude_sink(bus)

    # At rest (idle) -> amplitude is suppressed (no wire noise).
    bus.publish_state(OrbState.IDLE)
    seen.clear()
    sink(0.7)
    assert seen == []

    # While the user is speaking (listening) -> amplitude flows so the orb reacts.
    bus.publish_state(OrbState.LISTENING)
    seen.clear()
    sink(0.5)
    assert seen == [{"type": "amplitude", "value": 0.5}]

    # While Jack is talking -> amplitude flows too.
    bus.publish_state(OrbState.TALKING)
    seen.clear()
    sink(0.7)
    assert seen == [{"type": "amplitude", "value": 0.7}]


def test_voice_sink_publishes_listening_on_speech_and_idle_otherwise() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    sink = make_voice_sink(bus)

    sink(True)  # the user started speaking
    assert seen[-1] == {"type": "state", "value": "listening"}
    assert bus.last_state is OrbState.LISTENING

    sink(False)  # they stopped
    assert seen[-1] == {"type": "state", "value": "idle"}


def _serve_with_patched_deps(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> dict[str, Any]:
    """Run ``serve(settings)`` with all heavy/blocking deps stubbed; capture run_daemon kwargs.

    Patches the orchestrator build, the parent watchdog, the engine thread start, and
    ``run_daemon`` itself — so ``serve`` runs synchronously with no socket bound and no
    real engine constructed, and we can inspect exactly which callbacks it wired.
    """
    import threading as threading_mod

    import autobot.daemon.runner as runner_mod

    orchestrator = MagicMock()
    orchestrator.mcp_provider = None
    orchestrator.meeting_recorder = None

    monkeypatch.setattr("autobot.app.build", lambda *a, **k: orchestrator)
    monkeypatch.setattr("autobot.daemon.watchdog.start_parent_watchdog", lambda: None)
    monkeypatch.setattr("autobot.tools.confirm.ConfirmInbox", MagicMock)
    monkeypatch.setattr(threading_mod, "Thread", lambda **k: MagicMock())

    captured: dict[str, Any] = {}

    def fake_run_daemon(bus: Any, host: Any, port: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(runner_mod, "run_daemon", fake_run_daemon)

    runner_mod.serve(settings)
    return captured


def test_serve_disables_chat_but_wires_coder_session_callbacks_for_coder_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The coder daemon keeps /coder/turn + /coder/reply as its only *turn* entry point:
    # /chat and tool-run (/action) mutate the shared session/gate the coder driver guards
    # with its own lock, so they stay disabled — a concurrent /chat could otherwise route a
    # confirmation into a parked coder turn's channel. Session new/resume ARE wired for the
    # coder profile, but through the coder-driver-backed methods (which take that same driver
    # lock), so /new and /sessions resume are safe. /coder/undo + /coder/checkpoints are wired.
    settings = Settings(profile="coder")
    kwargs = _serve_with_patched_deps(monkeypatch, settings)

    assert kwargs["on_chat"] is None
    assert kwargs["on_action"] is None
    assert kwargs["on_new_session"] is not None  # coder-driver-backed (lock-safe)
    assert kwargs["on_resume_session"] is not None  # coder-driver-backed (lock-safe)
    assert kwargs["on_coder_turn"] is not None
    assert kwargs["on_coder_reply"] is not None
    assert kwargs["on_coder_undo"] is not None
    assert kwargs["on_coder_checkpoints"] is not None
    assert kwargs["on_list_sessions"] is not None  # read-only; the jack readiness probe
    assert kwargs["on_change"] is not None
    assert kwargs["on_confirm_answer"] is not None


def test_serve_keeps_chat_and_session_callbacks_for_assistant_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(profile="assistant")
    kwargs = _serve_with_patched_deps(monkeypatch, settings)

    assert kwargs["on_chat"] is not None
    assert kwargs["on_action"] is not None
    assert kwargs["on_new_session"] is not None
    assert kwargs["on_resume_session"] is not None
    assert kwargs["on_list_sessions"] is not None
