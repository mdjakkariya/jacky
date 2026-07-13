"""Orchestrator delegates coder-turn streaming calls to its driver (or errors when absent)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from autobot.orchestrator.state_machine import Orchestrator
from autobot.session_log import NullTranscript


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def start_stream(self, text: str) -> Iterator[dict[str, Any]]:
        self.calls.append(("start_stream", (text,)))
        yield {"status": "plan", "reply": "1. x", "todo": ["x"]}

    def reply_stream(self, value: str, text: str = "") -> Iterator[dict[str, Any]]:
        self.calls.append(("reply_stream", (value, text)))
        yield {"status": "done", "reply": "ok"}


def _bare_orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # no full build; exercise the two methods only
    orch.coder_driver = None
    orch._transcript = NullTranscript()  # coder-stream methods mirror events into it
    return orch


def test_delegates_to_driver() -> None:
    orch = _bare_orchestrator()
    driver = _FakeDriver()
    orch.coder_driver = driver  # type: ignore[assignment]
    assert list(orch.start_coder_stream("do it"))[-1]["status"] == "plan"
    assert list(orch.reply_coder_stream("approve"))[-1]["status"] == "done"
    assert driver.calls == [("start_stream", ("do it",)), ("reply_stream", ("approve", ""))]


class _StreamDriver:
    """Emits a tool line + plan on start, and a final reply on the gate answer."""

    def start_stream(self, text: str) -> Iterator[dict[str, Any]]:
        yield {
            "type": "tool",
            "event": "end",
            "name": "write_file",
            "ok": True,
            "label": "Edited f",
        }
        yield {"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]}

    def reply_stream(self, value: str, text: str = "") -> Iterator[dict[str, Any]]:
        yield {"status": "done", "reply": "Edited foo."}


class _RecordingTranscript(NullTranscript):
    """Captures the human-readable events written to it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def user(self, text: str, confidence: float) -> None:
        self.events.append(("user", text))

    def assistant(self, text: str) -> None:
        self.events.append(("assistant", text))

    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None:
        self.events.append(("tool", name, str(ok), detail))

    def note(self, text: str) -> None:
        self.events.append(("note", text))


def test_coder_stream_is_mirrored_into_the_transcript() -> None:
    # The coder's conversation must reach the readable transcript: the user turn, the plan,
    # tool activity, the gate answer, and the final reply — not just cloud-usage lines.
    orch = _bare_orchestrator()
    orch.coder_driver = _StreamDriver()  # type: ignore[assignment]
    tr = _RecordingTranscript()
    orch._transcript = tr
    list(orch.start_coder_stream("edit foo"))
    list(orch.reply_coder_stream("approve"))
    assert ("user", "edit foo") in tr.events
    assert ("tool", "write_file", "True", "Edited f") in tr.events
    assert ("assistant", "1. edit foo") in tr.events
    assert ("note", "gate answered: approve") in tr.events
    assert ("assistant", "Edited foo.") in tr.events


def test_refine_answer_recorded_as_a_user_turn() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = _StreamDriver()  # type: ignore[assignment]
    tr = _RecordingTranscript()
    orch._transcript = tr
    list(orch.reply_coder_stream("refine", "also update the docs"))
    assert ("user", "also update the docs") in tr.events


class _PendingDriver:
    def start_stream(self, text: str) -> Iterator[dict[str, Any]]:
        yield {"status": "pending", "prompt": "run pytest?"}


def test_pending_event_recorded_as_a_confirmation_note() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = _PendingDriver()  # type: ignore[assignment]
    tr = _RecordingTranscript()
    orch._transcript = tr
    list(orch.start_coder_stream("run the tests"))
    assert ("note", "awaiting confirmation: run pytest?") in tr.events


class _RaisingTranscript(NullTranscript):
    """Every write raises — the stream must survive it (best-effort transcript)."""

    def user(self, text: str, confidence: float) -> None:
        raise RuntimeError("disk full")

    def assistant(self, text: str) -> None:
        raise RuntimeError("disk full")


def test_transcript_write_failure_does_not_break_the_stream() -> None:
    # A transcript that raises on every write must not break the event stream — the CLI
    # still receives every event untouched.
    orch = _bare_orchestrator()
    orch.coder_driver = _StreamDriver()  # type: ignore[assignment]
    orch._transcript = _RaisingTranscript()
    events = list(orch.start_coder_stream("edit foo"))
    assert events[-1]["status"] == "plan"  # stream completed despite every write raising


def test_start_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.start_coder_stream("do it"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]


def test_reply_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.reply_coder_stream("approve"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]


class _UndoDrv:
    def undo(self) -> tuple[bool, str]:
        return True, "reverted"


def test_undo_coder_delegates_to_driver() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = _UndoDrv()  # type: ignore[assignment]
    assert orch.undo_coder() == (True, "reverted")


def test_undo_coder_noop_without_driver() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = None
    ok, _msg = orch.undo_coder()
    assert ok is False


class _ResumeNewDrv:
    def resume(self, sid: str) -> bool:
        return sid == "ok"

    def new_session(self) -> bool:
        return True

    def list_checkpoints(self) -> list[dict[str, str]]:
        return [{"ref": "refs/jack/checkpoints/0", "sha": "a", "label": "x"}]


def test_resume_and_new_coder_delegate() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = _ResumeNewDrv()  # type: ignore[assignment]
    assert orch.resume_coder_session("ok") is True
    assert orch.resume_coder_session("no") is False
    assert orch.new_coder_session() is True
    assert orch.list_coder_checkpoints()[0]["label"] == "x"


def test_list_and_resume_and_new_coder_noop_without_driver() -> None:
    orch = _bare_orchestrator()
    orch.coder_driver = None
    assert orch.list_coder_checkpoints() == []
    assert orch.resume_coder_session("anything") is False
    assert orch.new_coder_session() is False
