"""Unit tests for MeetingRecorder — all fakes, no hardware, no model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import numpy as np

from autobot.core.types import AudioClip, Segment, Transcription
from autobot.meeting.recorder import MeetingRecorder
from autobot.meeting.store import MeetingStore
from autobot.meeting.summarizer import MeetingSummarizer
from autobot.meeting.transcriber import MeetingTranscriber


class _FakeBranch:
    def __init__(self, n: int) -> None:
        self._frames = [np.full(512, 0.1, dtype=np.float32) for _ in range(n)]

    def frames(self):  # type: ignore[no-untyped-def]
        yield from self._frames


class _FakeFar(_FakeBranch):
    aec_active = False

    def close(self) -> None:
        """Stop the fake far source."""
        pass


class _FakeSTT:
    """Fake STT implementing the full SpeechToText protocol (transcribe + transcribe_segments)."""

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Fake transcribe returning a fixed Transcription."""
        return Transcription(text="hello world", confidence=1.0)

    def transcribe_segments(self, audio: AudioClip, **kw: object) -> list[Segment]:
        """Fake transcribe_segments returning one segment when audio is non-empty."""
        return [Segment("hello world", 0.0, 1.0)] if audio.size else []


def _fake_summarizer() -> MeetingSummarizer:
    """Return a MeetingSummarizer backed by a simple completer."""
    return MeetingSummarizer(complete=lambda prompt: "## Summary\n- ok\n", max_chars=8000)


def _recorder(tmp_path: Path, far_ok: bool = True) -> MeetingRecorder:
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    def far_factory():  # type: ignore[no-untyped-def]
        if not far_ok:
            raise RuntimeError("audio capture denied")
        return _FakeFar(8)

    return MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=far_factory,
        keep_audio=True,
    )


def test_full_lifecycle_writes_files(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    ack = rec.start("Standup")
    assert "recording" in ack.lower()
    assert rec.status()["active"] is True and rec.status()["mic_only"] is False
    out = rec.stop()
    assert "saved" in out.lower()
    assert rec.status()["active"] is False


def test_degrades_to_mic_only(tmp_path: Path) -> None:
    rec = _recorder(tmp_path, far_ok=False)
    ack = rec.start("Solo")
    assert "your side" in ack.lower() or "mic-only" in ack.lower()
    assert rec.status()["mic_only"] is True
    rec.stop()


def test_refuses_double_start(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.start("A")
    assert "already" in rec.start("B").lower()
    rec.stop()


def test_pause_resume(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.start("A")
    assert "paused" in rec.pause().lower()
    assert "paused" in rec.pause().lower()  # idempotent message
    assert "resum" in rec.resume().lower()
    rec.stop()


def test_stop_with_nothing_active(tmp_path: Path) -> None:
    assert "no meeting" in _recorder(tmp_path).stop().lower()


def test_far_stream_failure_sets_interrupted(tmp_path: Path) -> None:
    """Far stream that raises mid-run sets far_stream=interrupted in manifest."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    class _FailingFar:
        aec_active = False

        def frames(self):  # type: ignore[no-untyped-def]
            yield np.zeros(512, dtype=np.float32)
            raise RuntimeError("sidecar died")

        def close(self) -> None:
            """Stop the failing far source."""
            pass

    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=lambda: _FailingFar(),
        keep_audio=True,
    )
    rec.start("BoardMeeting")
    out = rec.stop()
    assert "saved" in out.lower()

    # Read manifest from the created meeting dir
    meetings = list(Path(tmp_path).iterdir())
    assert meetings, "no meeting folder created"
    manifest = json.loads((meetings[0] / "manifest.json").read_text())
    assert manifest["far_stream"]["status"] == "interrupted"


def test_resummarize_with_transcript(tmp_path: Path) -> None:
    """Resummarize reads transcript.md and rewrites minutes.md."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=lambda: _FakeFar(8),
        keep_audio=True,
    )

    # Create a meeting with a transcript
    rec.start("Retro")
    rec.stop()

    meetings = list(Path(tmp_path).iterdir())
    assert meetings
    meeting_dir = meetings[0]
    meeting_id = meeting_dir.name

    # Overwrite minutes.md so we can verify it gets rebuilt
    (meeting_dir / "minutes.md").write_text("old minutes")

    result = rec.resummarize(meeting_id)
    assert "rebuilt" in result.lower() or meeting_id in result

    new_minutes = (meeting_dir / "minutes.md").read_text()
    assert new_minutes != "old minutes"


def test_resummarize_no_id_uses_most_recent(tmp_path: Path) -> None:
    """Resummarize(None) targets the most recent meeting."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=lambda: _FakeFar(8),
        keep_audio=True,
    )
    rec.start("Sprint")
    rec.stop()

    result = rec.resummarize(None)
    assert "rebuilt" in result.lower() or "minutes" in result.lower()


def test_resummarize_no_meetings(tmp_path: Path) -> None:
    """Resummarize(None) with no meetings returns friendly message."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=lambda: _FakeFar(8),
        keep_audio=True,
    )
    result = rec.resummarize(None)
    assert "no meeting" in result.lower()
