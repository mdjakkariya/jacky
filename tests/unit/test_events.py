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

    bus.publish_confirm("Empty the Trash?")  # voice + destructive by default
    assert seen[-1] == {
        "type": "confirm",
        "text": "Empty the Trash?",
        "mode": "voice",
        "kind": "danger",
        "options": None,
    }

    bus.publish_confirm("Empty the Trash?", chat=True)  # chat turn -> orb ignores it
    assert seen[-1] == {
        "type": "confirm",
        "text": "Empty the Trash?",
        "mode": "chat",
        "kind": "danger",
        "options": None,
    }

    # A read grant: calm tone + an access-level dropdown to pick from.
    levels = [{"label": "Read only", "value": "read"}, {"label": "Read & write", "value": "write"}]
    bus.publish_confirm("Let Jack read files in ~/proj?", chat=True, kind="read", options=levels)
    assert seen[-1]["kind"] == "read" and seen[-1]["options"] == levels


def test_publish_context_carries_pct_and_dev_flag() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_context(
        {
            "used": 50_000,
            "window": 200_000,
            "model": "claude-haiku-4-5",
            "cache_read": 40_000,
            "cache_write": 1_000,
            "turn_in": 3_426,
            "turn_out": 23,
        },
        dev=True,
    )
    assert seen[-1] == {
        "type": "context",
        "used": 50_000,
        "window": 200_000,
        "pct": 25,
        "dev": True,
        "model": "claude-haiku-4-5",
        "cache_read": 40_000,
        "cache_write": 1_000,
        "turn_in": 3_426,
        "turn_out": 23,
        "price": None,  # absent from info -> None (UI hides the row)
    }


def test_publish_context_carries_session_price() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_context(
        {"used": 50_000, "window": 200_000, "model": "claude-haiku-4-5", "price": 0.0123},
    )
    assert seen[-1]["price"] == 0.0123


def test_publish_context_price_defaults_to_none_when_absent() -> None:
    # Local (Ollama) reports no price; the wire carries None so the cost row hides.
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_context({"used": 10, "window": 100})
    assert seen[-1]["price"] is None


def test_publish_choices_carries_items_and_chat_mode() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    items = [{"label": "a.pdf", "actions": [{"label": "Open", "tool": "open_path", "args": {}}]}]
    bus.publish_choices("Files matching 'a'", items)
    assert seen[-1] == {
        "type": "choices",
        "title": "Files matching 'a'",
        "items": items,
        "mode": "chat",
    }


def test_publish_choices_voice_mode_is_tagged_so_chat_drawer_ignores_it() -> None:
    # A search run in voice mode must publish mode "voice"; the chat drawer renders
    # only chat-mode choices, so this keeps stray cards out of an empty chat.
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_choices("Files matching 'a'", [{"label": "a.pdf"}], chat=False)
    assert seen[-1]["mode"] == "voice"


def test_publish_voice_download_carries_pct_and_done() -> None:
    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_voice_download(0.5, "Downloading voice…")
    assert seen[-1] == {
        "type": "voice_download",
        "fraction": 0.5,
        "pct": 50,
        "stage": "Downloading voice…",
        "done": False,
        "error": "",
    }
    bus.publish_voice_download(1.0, "Ready", done=True)
    assert seen[-1]["done"] is True and seen[-1]["pct"] == 100


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


def test_publish_step_emits_running_then_done() -> None:
    from autobot.core.events import EventBus

    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_step(0, "search_files", "Searching files", "running")
    bus.publish_step(0, "search_files", "Searching files", "done")
    assert seen == [
        {
            "type": "step",
            "index": 0,
            "tool": "search_files",
            "label": "Searching files",
            "status": "running",
        },
        {
            "type": "step",
            "index": 0,
            "tool": "search_files",
            "label": "Searching files",
            "status": "done",
        },
    ]


def test_publish_workspace_emits_and_remembers_last() -> None:
    from autobot.core.events import EventBus

    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_workspace("/Users/me/proj", "proj")
    assert seen == [{"type": "workspace", "path": "/Users/me/proj", "name": "proj"}]
    assert bus.last_workspace == {"type": "workspace", "path": "/Users/me/proj", "name": "proj"}


def test_publish_mcp_reaches_subscriber() -> None:
    bus = EventBus()
    received: list[dict[str, object]] = []
    bus.subscribe(received.append)

    payload: dict[str, object] = {
        "type": "mcp_status",
        "server": "slack",
        "state": "connected",
        "tool_count": 7,
    }
    bus.publish_mcp(payload)

    assert received == [payload]


def test_publish_mcp_passes_payload_unchanged() -> None:
    bus = EventBus()
    received: list[dict[str, object]] = []
    bus.subscribe(received.append)

    # Any dict shape flows through — publish_mcp is a typed passthrough
    oauth_payload: dict[str, object] = {"type": "mcp_oauth", "server": "github", "url": "https://x"}
    bus.publish_mcp(oauth_payload)

    assert len(received) == 1
    assert received[0]["type"] == "mcp_oauth"
