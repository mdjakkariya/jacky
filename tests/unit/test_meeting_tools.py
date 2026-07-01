"""Tests for meeting tools (design §8)."""

from __future__ import annotations

from autobot import permissions
from autobot.core.types import Risk
from autobot.tools.meeting import MeetingTools
from autobot.tools.registry import ToolSpec


class _FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, title: str) -> str:
        self.calls.append(f"start:{title}")
        return "Recording the meeting."

    def stop(self) -> str:
        return "Saved the meeting minutes to /x."

    def status(self) -> dict:  # type: ignore[type-arg]
        return {"active": False}


def _specs() -> dict[str, ToolSpec]:
    return {s.name: s for s in MeetingTools(_FakeRecorder()).specs()}  # type: ignore[arg-type]


def test_start_requires_microphone_and_is_write() -> None:
    spec = _specs()["start_meeting"]
    assert spec.requires == permissions.MICROPHONE
    assert spec.risk == Risk.WRITE


def test_status_is_read_only() -> None:
    assert _specs()["meeting_status"].risk == Risk.READ_ONLY


def test_handlers_return_strings() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    assert "recording" in tools.start_meeting(title="Standup").lower()
    assert rec.calls == ["start:Standup"]
