"""Tests for the daemon runner's state/amplitude adapters.

Skipped when the optional ``daemon`` extra is absent (runner imports the server).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

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
