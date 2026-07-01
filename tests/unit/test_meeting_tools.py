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
        self.calls.append("stop")
        return "Saved the meeting minutes to /x."

    def pause(self) -> str:
        self.calls.append("pause")
        return "Paused the recording."

    def resume(self) -> str:
        self.calls.append("resume")
        return "Resumed the recording."

    def status(self) -> dict:  # type: ignore[type-arg]
        self.calls.append("status")
        return {"active": False}

    def resummarize(self, meeting_id: str | None) -> str:
        self.calls.append(f"resummarize:{meeting_id}")
        return f"Rebuilt the minutes for {meeting_id}."

    def list_recent(self) -> list[dict[str, object]]:
        self.calls.append("list_recent")
        return [
            {
                "id": "2026-07-01-2225-standup",
                "title": "Standup",
                "state": "done",
                "dir": "/x/meetings/2026-07-01-2225-standup",
            }
        ]

    def last_minutes(self) -> dict[str, object] | None:
        self.calls.append("last_minutes")
        return {
            "id": "2026-07-01-2225-standup",
            "dir": "/x/meetings/2026-07-01-2225-standup",
            "mic_only": True,
            "minutes_md": (
                "# Standup\n\n## Summary\nShipped the reveal button.\n\n## Decisions\n- None\n"
            ),
        }

    def delete(self, meeting_id: str) -> dict[str, object]:
        self.calls.append(f"delete:{meeting_id}")
        return {"ok": True, "id": meeting_id}


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


# --- Risk / requires assertions ---


def test_stop_pause_resume_summarize_are_write() -> None:
    specs = _specs()
    for name in ("stop_meeting", "pause_meeting", "resume_meeting", "summarize_meeting"):
        assert specs[name].risk == Risk.WRITE, f"{name} should be WRITE"


def test_status_and_list_are_read_only() -> None:
    specs = _specs()
    for name in ("meeting_status", "list_meetings"):
        assert specs[name].risk == Risk.READ_ONLY, f"{name} should be READ_ONLY"


def test_requires_is_none_for_all_except_start() -> None:
    specs = _specs()
    for name in (
        "stop_meeting",
        "pause_meeting",
        "resume_meeting",
        "meeting_status",
        "list_meetings",
        "summarize_meeting",
    ):
        assert specs[name].requires is None, f"{name}.requires should be None"
    assert specs["start_meeting"].requires == permissions.MICROPHONE


# --- Delegation and return-type assertions ---


def test_list_meetings_calls_list_recent_and_returns_string() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.list_meetings()
    assert "list_recent" in rec.calls
    assert isinstance(result, str)
    assert "Standup" in result


def test_list_meetings_includes_the_folder_path() -> None:
    """The user must be able to learn WHERE a meeting is saved."""
    result = MeetingTools(_FakeRecorder()).list_meetings()  # type: ignore[arg-type]
    assert "/x/meetings/2026-07-01-2225-standup" in result


def test_last_meeting_reports_path_and_summary() -> None:
    rec = _FakeRecorder()
    result = MeetingTools(rec).last_meeting()  # type: ignore[arg-type]
    assert "last_minutes" in rec.calls
    assert "/x/meetings/2026-07-01-2225-standup" in result
    assert "Standup" in result
    assert "Shipped the reveal button" in result


def test_last_meeting_when_none_saved() -> None:
    class _Empty(_FakeRecorder):
        def last_minutes(self) -> dict[str, object] | None:
            return None

    result = MeetingTools(_Empty()).last_meeting()  # type: ignore[arg-type]
    assert "no saved meetings" in result.lower()


def test_last_meeting_is_core_and_read_only() -> None:
    spec = _specs()["last_meeting"]
    assert spec.core is True
    assert spec.risk == Risk.READ_ONLY


def test_delete_meeting_with_id_delegates() -> None:
    rec = _FakeRecorder()
    result = MeetingTools(rec).delete_meeting("2026-07-01-2225-standup")  # type: ignore[arg-type]
    assert "delete:2026-07-01-2225-standup" in rec.calls
    assert "2026-07-01-2225-standup" in result


def test_delete_meeting_without_id_targets_most_recent() -> None:
    rec = _FakeRecorder()
    MeetingTools(rec).delete_meeting()  # type: ignore[arg-type]
    assert "last_minutes" in rec.calls
    assert "delete:2026-07-01-2225-standup" in rec.calls


def test_delete_meeting_reports_failure() -> None:
    class _Fails(_FakeRecorder):
        def delete(self, meeting_id: str) -> dict[str, object]:
            return {"ok": False, "error": "meeting not found"}

    out = MeetingTools(_Fails()).delete_meeting("nope")  # type: ignore[arg-type]
    assert "couldn't" in out.lower()


def test_delete_meeting_is_destructive_and_confirms() -> None:
    spec = _specs()["delete_meeting"]
    assert spec.risk == Risk.DESTRUCTIVE
    assert spec.confirm_prompt


# --- handler branch coverage ---


def test_meeting_status_active_reports_minutes() -> None:
    class _Active(_FakeRecorder):
        def status(self) -> dict:  # type: ignore[type-arg]
            return {
                "active": True,
                "paused": False,
                "mic_only": False,
                "elapsed_s": 125.0,
                "title": "Sync",
            }

    out = MeetingTools(_Active()).meeting_status()  # type: ignore[arg-type]
    assert "Sync" in out and "2 min" in out


def test_meeting_status_paused_and_miconly_suffixes() -> None:
    class _Paused(_FakeRecorder):
        def status(self) -> dict:  # type: ignore[type-arg]
            return {
                "active": True,
                "paused": True,
                "mic_only": False,
                "elapsed_s": 60.0,
                "title": "P",
            }

    class _Mic(_FakeRecorder):
        def status(self) -> dict:  # type: ignore[type-arg]
            return {
                "active": True,
                "paused": False,
                "mic_only": True,
                "elapsed_s": 30.0,
                "title": "M",
            }

    assert "(paused)" in MeetingTools(_Paused()).meeting_status()  # type: ignore[arg-type]
    assert "mic-only" in MeetingTools(_Mic()).meeting_status()  # type: ignore[arg-type]


def test_list_meetings_empty() -> None:
    class _Empty(_FakeRecorder):
        def list_recent(self) -> list[dict[str, object]]:
            return []

    assert "no saved meetings" in MeetingTools(_Empty()).list_meetings().lower()  # type: ignore[arg-type]


def test_last_meeting_handles_lookup_error() -> None:
    class _Boom(_FakeRecorder):
        def last_minutes(self) -> dict[str, object] | None:
            raise RuntimeError("boom")

    assert "couldn't" in MeetingTools(_Boom()).last_meeting().lower()  # type: ignore[arg-type]


def test_last_meeting_without_summary_still_reports_path() -> None:
    class _NoSum(_FakeRecorder):
        def last_minutes(self) -> dict[str, object] | None:
            return {"id": "m", "dir": "/x/m", "mic_only": True, "minutes_md": "# Title only\n"}

    out = MeetingTools(_NoSum()).last_meeting()  # type: ignore[arg-type]
    assert "Title only" in out and "/x/m" in out and "Summary:" not in out


def test_delete_meeting_lookup_error() -> None:
    class _Boom(_FakeRecorder):
        def last_minutes(self) -> dict[str, object] | None:
            raise RuntimeError("boom")

    assert "couldn't find" in MeetingTools(_Boom()).delete_meeting().lower()  # type: ignore[arg-type]


def test_delete_meeting_empty_when_none_saved() -> None:
    class _NoneSaved(_FakeRecorder):
        def last_minutes(self) -> dict[str, object] | None:
            return None

    assert "no saved meetings" in MeetingTools(_NoneSaved()).delete_meeting().lower()  # type: ignore[arg-type]


def test_summarize_meeting_with_id_calls_resummarize() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.summarize_meeting("my-meeting")
    assert "resummarize:my-meeting" in rec.calls
    assert isinstance(result, str)


def test_summarize_meeting_without_id_calls_resummarize_none() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.summarize_meeting()
    assert "resummarize:None" in rec.calls
    assert isinstance(result, str)


def test_stop_meeting_returns_string() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.stop_meeting()
    assert "stop" in rec.calls
    assert isinstance(result, str)


def test_pause_meeting_returns_string() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.pause_meeting()
    assert "pause" in rec.calls
    assert isinstance(result, str)


def test_resume_meeting_returns_string() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.resume_meeting()
    assert "resume" in rec.calls
    assert isinstance(result, str)


def test_meeting_status_returns_string() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)  # type: ignore[arg-type]
    result = tools.meeting_status()
    assert "status" in rec.calls
    assert isinstance(result, str)


def test_meeting_status_never_raises_on_bad_status() -> None:
    """meeting_status must return a string even when status() raises."""

    class _BrokenRecorder(_FakeRecorder):
        def status(self) -> dict:  # type: ignore[type-arg]
            raise RuntimeError("simulated failure")

    tools = MeetingTools(_BrokenRecorder())  # type: ignore[arg-type]
    result = tools.meeting_status()
    assert isinstance(result, str)
    assert "couldn't" in result.lower() or "check" in result.lower()
