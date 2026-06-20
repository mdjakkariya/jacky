"""Tests for the engine→UI event seam (orb states + the event bus)."""

from __future__ import annotations

from autobot.core.events import (
    AmplitudeEvent,
    EventBus,
    OrbState,
    StateEvent,
    orb_state_for,
)
from autobot.core.types import State


def test_every_internal_state_maps_to_an_orb_state() -> None:
    # Exhaustive: a new State must be given an explicit mapping (no KeyError).
    for state in State:
        assert isinstance(orb_state_for(state), OrbState)


def test_state_collapsing_is_what_we_expect() -> None:
    assert orb_state_for(State.IDLE) is OrbState.IDLE
    assert orb_state_for(State.LISTENING) is OrbState.LISTENING
    assert orb_state_for(State.TRANSCRIBING) is OrbState.THINKING
    assert orb_state_for(State.PLANNING) is OrbState.THINKING
    assert orb_state_for(State.EXECUTING) is OrbState.THINKING
    assert orb_state_for(State.RESPONDING) is OrbState.TALKING
    assert orb_state_for(State.CLARIFYING) is OrbState.TALKING
    assert orb_state_for(State.ERROR) is OrbState.IDLE


def test_event_messages_have_the_wire_shape() -> None:
    assert StateEvent(OrbState.LISTENING).message() == {"type": "state", "value": "listening"}
    assert AmplitudeEvent(0.5).message() == {"type": "amplitude", "value": 0.5}


def test_bus_broadcasts_state_to_subscribers_and_records_last() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)

    bus.publish_state(OrbState.THINKING)

    assert seen == [{"type": "state", "value": "thinking"}]
    assert bus.last_state is OrbState.THINKING


def test_bus_clamps_amplitude_to_unit_range() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)

    bus.publish_amplitude(-2.0)
    bus.publish_amplitude(5.0)
    bus.publish_amplitude(0.25)

    assert [m["value"] for m in seen] == [0.0, 1.0, 0.25]


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    unsubscribe = bus.subscribe(seen.append)

    bus.publish_state(OrbState.LISTENING)
    unsubscribe()
    bus.publish_state(OrbState.IDLE)

    assert seen == [{"type": "state", "value": "listening"}]


def test_multiple_subscribers_each_receive_events() -> None:
    bus = EventBus()
    a: list[dict[str, object]] = []
    b: list[dict[str, object]] = []
    bus.subscribe(a.append)
    bus.subscribe(b.append)

    bus.publish_state(OrbState.TALKING)

    assert a == b == [{"type": "state", "value": "talking"}]
