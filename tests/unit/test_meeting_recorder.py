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


def _recorder(
    tmp_path: Path,
    far_ok: bool = True,
    on_event: object = None,
) -> MeetingRecorder:
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
        on_event=on_event,  # type: ignore[arg-type]
    )


def test_full_lifecycle_writes_files(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    ack = rec.start("Standup")
    assert "recording" in ack.lower()
    assert rec.status()["active"] is True and rec.status()["mic_only"] is False
    out = rec.stop()
    assert "saved" in out.lower()
    assert rec.status()["active"] is False


def test_emits_full_phase_sequence(tmp_path: Path) -> None:
    """start→stop emits the full phase sequence, never a trailing 'idle'.

    Expected order: recording → transcribing → summarizing → done. A trailing
    'idle' would wipe the drawer's minutes card.

    Regression: _finalize runs after self._active is cleared, so emitting via
    status() reported 'idle' for every finalize phase and the drawer never showed
    the processing or minutes cards.
    """
    events: list[str] = []
    rec = _recorder(tmp_path, on_event=lambda s: events.append(str(s["state"])))
    rec.start("Standup")
    rec.stop()

    # The recording event fires first, then the three finalize phases in order.
    assert events[0] == "recording"
    assert events[-1] == "done", f"last event must be 'done', got {events!r}"
    assert "idle" not in events, f"no 'idle' during finalize (would clear cards): {events!r}"
    # transcribing precedes summarizing precedes done.
    assert events.index("transcribing") < events.index("summarizing") < events.index("done")


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


def test_resummarize_without_transcript_rebuilds_from_wavs(tmp_path: Path) -> None:
    """Resummarize rebuilds transcript.md from WAVs when transcript.md is absent."""
    rec = _recorder(tmp_path, far_ok=True)

    # Run a full meeting (keep_audio=True so WAVs are preserved)
    rec.start("Design Review")
    rec.stop()

    meetings = list(Path(tmp_path).iterdir())
    assert meetings, "no meeting folder created"
    meeting_dir = meetings[0]
    meeting_id = meeting_dir.name

    # Delete transcript.md to force the WAV-rebuild path
    transcript_path = meeting_dir / "transcript.md"
    assert transcript_path.exists(), "transcript.md should exist after stop()"
    transcript_path.unlink()
    assert not transcript_path.exists()

    result = rec.resummarize(meeting_id)
    assert "rebuilt" in result.lower() or meeting_id in result

    # Both transcript.md and minutes.md must exist after rebuilding
    assert transcript_path.exists(), "transcript.md should be rebuilt from WAVs"
    assert (meeting_dir / "minutes.md").exists(), "minutes.md should be written"


def test_near_source_close_called_after_stop(tmp_path: Path) -> None:
    """The near-branch source's close() is called when the meeting stops (mic released)."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    close_called = False

    class _TrackingBranch:
        """Fake branch that tracks whether close() was called."""

        def frames(self):  # type: ignore[no-untyped-def]
            yield from [np.full(512, 0.1, dtype=np.float32) for _ in range(4)]

        def close(self) -> None:
            nonlocal close_called
            close_called = True

    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _TrackingBranch(),
        far_source_factory=lambda: _FakeFar(4),
        keep_audio=True,
    )
    rec.start("CloseTest")
    rec.stop()

    assert close_called, "near source's close() must be called after meeting stop (mic released)"


def test_finalize_interrupted_recovers_recording_state(tmp_path: Path) -> None:
    """finalize_interrupted() recovers a meeting left mid-flight (state='recording')."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    # Manually create a meeting folder that looks like it was interrupted mid-recording
    from autobot.meeting.wav import WavWriter

    meeting_id = "2026-01-01-1200-interrupted"
    meeting_dir = Path(tmp_path) / meeting_id
    meeting_dir.mkdir()

    # Write a minimal near.wav so the transcriber has something to read
    near_wav_path = str(meeting_dir / "near.wav")
    writer = WavWriter(near_wav_path)
    import numpy as np

    writer.append(np.full(512, 0.05, dtype=np.float32))
    writer.close()

    # Write a manifest in state="recording" (simulates a crash)
    manifest_data = {
        "id": meeting_id,
        "title": "Interrupted Meeting",
        "started_at": "2026-01-01T12:00:00",
        "state": "recording",
        "mic_only": True,
        "far_stream": {"status": "unavailable"},
        "pauses": [],
    }
    paths = store._paths(meeting_id)
    store.write_manifest(paths, manifest_data)

    # Build a fresh recorder — simulates a restart after crash
    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(0),
        far_source_factory=lambda: _FakeFar(0),
        keep_audio=True,
    )

    recovered = rec.finalize_interrupted()
    assert meeting_id in recovered, "meeting should be recovered"

    # Manifest must now be state="done"
    manifest = store.read_manifest(str(meeting_dir))
    assert manifest.get("state") == "done", f"expected state=done, got {manifest.get('state')}"

    # minutes.md must exist
    assert (meeting_dir / "minutes.md").exists(), "minutes.md should be written after recovery"


def test_finalize_interrupted_terminal_on_summarizer_failure(tmp_path: Path) -> None:
    """finalize_interrupted() marks state=done even when the summarizer raises.

    This ensures a crashed meeting is never re-processed on subsequent startups
    (no infinite retry when the LLM is offline at startup time).
    """
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]

    def _raising_complete(prompt: str) -> str:
        raise RuntimeError("LLM offline")

    failing_summarizer = MeetingSummarizer(complete=_raising_complete, max_chars=8000)

    from autobot.meeting.wav import WavWriter

    meeting_id = "2026-01-01-1300-summarizer-fail"
    meeting_dir = Path(tmp_path) / meeting_id
    meeting_dir.mkdir()

    near_wav_path = str(meeting_dir / "near.wav")
    writer = WavWriter(near_wav_path)
    writer.append(np.full(512, 0.05, dtype=np.float32))
    writer.close()

    manifest_data = {
        "id": meeting_id,
        "title": "SummaryFail Meeting",
        "started_at": "2026-01-01T13:00:00",
        "state": "recording",
        "mic_only": True,
        "far_stream": {"status": "unavailable"},
        "pauses": [],
    }
    paths = store._paths(meeting_id)
    store.write_manifest(paths, manifest_data)

    rec = MeetingRecorder(
        store,
        tr,
        failing_summarizer,
        near_branch_factory=lambda: _FakeBranch(0),
        far_source_factory=lambda: _FakeFar(0),
        keep_audio=True,
    )

    recovered = rec.finalize_interrupted()
    assert meeting_id in recovered, "meeting must still appear in recovered list"

    # Manifest MUST be state="done" even though summary raised.
    manifest = store.read_manifest(str(meeting_dir))
    assert manifest.get("state") == "done", (
        f"expected state=done after summary failure, got {manifest.get('state')}"
    )

    # A fallback minutes.md must exist (not empty).
    minutes_path = meeting_dir / "minutes.md"
    assert minutes_path.exists(), "fallback minutes.md must be written on summary failure"
    assert minutes_path.read_text(encoding="utf-8").strip(), "fallback minutes.md must not be empty"


# ---------------------------------------------------------------------------
# last_minutes() tests (RED — method not yet implemented)
# ---------------------------------------------------------------------------


def test_last_minutes_returns_dict_with_minutes_md(tmp_path: Path) -> None:
    """last_minutes() returns a dict with id, dir, mic_only, minutes_md for the newest meeting."""
    rec = _recorder(tmp_path)
    rec.start("Budget Review")
    rec.stop()

    result = rec.last_minutes()
    assert result is not None, "expected a dict, got None"
    assert "id" in result
    assert "dir" in result
    assert isinstance(result["mic_only"], bool)
    minutes_text = result["minutes_md"]
    assert isinstance(minutes_text, str) and minutes_text.strip(), "minutes_md must be non-empty"


def test_last_minutes_returns_none_when_no_meetings(tmp_path: Path) -> None:
    """last_minutes() returns None when there are no finished meetings."""
    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]
    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(0),
        far_source_factory=lambda: _FakeFar(0),
        keep_audio=True,
    )
    assert rec.last_minutes() is None


def test_last_minutes_skips_meeting_without_minutes_md(tmp_path: Path) -> None:
    """last_minutes() skips a meeting folder that has no minutes.md and returns None."""
    store = MeetingStore(str(tmp_path))
    # Create a meeting folder that has a manifest but no minutes.md
    paths = store.create("Empty Meeting")
    store.write_manifest(
        paths,
        {
            "id": paths.id,
            "title": "Empty Meeting",
            "started_at": "2026-01-01T10:00:00",
            "state": "done",
            "mic_only": False,
            "far_stream": {"status": "ok"},
            "pauses": [],
        },
    )
    # minutes.md is intentionally NOT written — simulates a partially-finalized meeting

    tr = MeetingTranscriber(cast("object", _FakeSTT()), chunk_s=30.0, overlap_s=3.0, stt_prompt="")  # type: ignore[arg-type]
    rec = MeetingRecorder(
        store,
        tr,
        _fake_summarizer(),
        near_branch_factory=lambda: _FakeBranch(0),
        far_source_factory=lambda: _FakeFar(0),
        keep_audio=True,
    )
    assert rec.last_minutes() is None
