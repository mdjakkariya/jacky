"""Tests for MeetingEvent and EventBus.publish_meeting."""

from __future__ import annotations

from autobot.core.events import EventBus, MeetingEvent


def test_message_shape() -> None:
    msg = MeetingEvent(
        state="recording",
        elapsed_s=12.0,
        recorded_s=10.0,
        mic_only=False,
        paused=False,
        title="Standup",
    ).message()
    assert msg["type"] == "meeting" and msg["state"] == "recording" and msg["title"] == "Standup"


def test_publish_meeting_fans_out() -> None:
    bus = EventBus()
    seen: list[dict] = []  # type: ignore[type-arg]
    bus.subscribe(seen.append)
    bus.publish_meeting(
        {
            "state": "recording",
            "elapsed_s": 1.0,
            "recorded_s": 1.0,
            "mic_only": False,
            "paused": False,
            "title": "x",
        }
    )
    assert seen and seen[-1]["type"] == "meeting"
