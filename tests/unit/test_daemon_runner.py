"""Tests for the daemon runner's state/amplitude adapters.

Skipped when the optional ``daemon`` extra is absent (runner imports the server).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from autobot.core.events import EventBus, OrbState
from autobot.core.types import State
from autobot.daemon.runner import make_amplitude_sink, make_state_listener


def test_state_listener_publishes_mapped_orb_state() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    listener = make_state_listener(bus)

    listener(State.IDLE, State.PLANNING)

    assert seen == [{"type": "state", "value": "thinking"}]
    assert bus.last_state is OrbState.THINKING


def test_amplitude_sink_only_emits_while_talking() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    sink = make_amplitude_sink(bus)

    # Passive listening maps to idle -> amplitude is suppressed (no wire noise).
    bus.publish_state(OrbState.IDLE)
    seen.clear()
    sink(0.7)
    assert seen == []

    # While talking, amplitude flows so the orb pulses with speech.
    bus.publish_state(OrbState.TALKING)
    seen.clear()
    sink(0.7)
    assert seen == [{"type": "amplitude", "value": 0.7}]
