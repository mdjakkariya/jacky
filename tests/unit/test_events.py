"""Tests for the engine→UI event seam (orb states + the event bus)."""

from __future__ import annotations

from autobot.core.events import (
    AmplitudeEvent,
    EventBus,
    OrbState,
    StateEvent,
    VisibilityEvent,
    orb_state_for,
)
from autobot.core.types import State


def test_every_internal_state_maps_to_an_orb_state() -> None:
    # Exhaustive: a new State must be given an explicit mapping (no KeyError).
    for state in State:
        assert isinstance(orb_state_for(state), OrbState)


def test_state_collapsing_is_what_we_expect() -> None:
    # Passive capture/transcribe read as IDLE so the orb rests until the
    # assistant is genuinely engaged with an addressed turn.
    assert orb_state_for(State.IDLE) is OrbState.IDLE
    assert orb_state_for(State.LISTENING) is OrbState.IDLE
    assert orb_state_for(State.TRANSCRIBING) is OrbState.IDLE
    assert orb_state_for(State.PLANNING) is OrbState.THINKING
    assert orb_state_for(State.EXECUTING) is OrbState.THINKING
    assert orb_state_for(State.RESPONDING) is OrbState.TALKING
    assert orb_state_for(State.CLARIFYING) is OrbState.TALKING
    assert orb_state_for(State.ERROR) is OrbState.IDLE


def test_event_messages_have_the_wire_shape() -> None:
    assert StateEvent(OrbState.LISTENING).message() == {"type": "state", "value": "listening"}
    assert AmplitudeEvent(0.5).message() == {"type": "amplitude", "value": 0.5}
    assert VisibilityEvent(visible=False).message() == {"type": "visibility", "value": "hide"}
    assert VisibilityEvent(visible=True).message() == {"type": "visibility", "value": "show"}


def test_publish_visibility_broadcasts_without_changing_last_state() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)

    bus.publish_state(OrbState.TALKING)
    bus.publish_visibility(visible=False)

    assert seen[-1] == {"type": "visibility", "value": "hide"}
    # A visibility request is not a state — last_state is unaffected.
    assert bus.last_state is OrbState.TALKING


def test_publish_confirm_carries_mode_so_the_orb_can_ignore_chat_cards() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)

    bus.publish_confirm("Empty the Trash?")  # voice by default
    assert seen[-1] == {"type": "confirm", "text": "Empty the Trash?", "mode": "voice"}

    bus.publish_confirm("Empty the Trash?", chat=True)  # chat turn -> orb ignores it
    assert seen[-1] == {"type": "confirm", "text": "Empty the Trash?", "mode": "chat"}


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
