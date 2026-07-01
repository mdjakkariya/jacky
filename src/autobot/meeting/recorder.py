"""Owns a meeting's capture threads and the stop→transcribe→summarize finalize.

Capture writes only to disk (no STT/LLM while recording), so a long meeting costs
constant RAM and survives a crash. Sources + transcriber + summarizer are injected
so this is unit-tested with fakes (design §4.2, §5).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from autobot.core.interfaces import SystemAudioSource
from autobot.logging_setup import get_logger
from autobot.meeting.store import MeetingPaths, MeetingStore
from autobot.meeting.summarizer import MeetingSummarizer
from autobot.meeting.transcriber import MeetingTranscriber
from autobot.meeting.wav import WavWriter, repair_header

_log = get_logger("meeting")


class _StreamWriter:
    """Drains a frame iterator into a WavWriter on its own thread, pausable."""

    def __init__(self, source: object, wav_path: str) -> None:
        self._source = source
        self._writer = WavWriter(wav_path)
        self._paused = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.failed = False

    def start(self) -> None:
        """Start the writer thread."""
        self._thread.start()

    def _run(self) -> None:
        try:
            for frame in self._source.frames():  # type: ignore[attr-defined]
                if self._stopped.is_set():
                    break
                if not self._paused.is_set():
                    self._writer.append(frame)
        except Exception:
            self.failed = True
            _log.exception("stream writer error path=%s", self._writer)
        finally:
            self._writer.close()

    def pause(self) -> None:
        """Pause writing (frames are discarded until resume)."""
        self._paused.set()

    def resume(self) -> None:
        """Resume writing."""
        self._paused.clear()

    def stop(self) -> None:
        """Signal stop, close the source, and join the thread.

        The writer is closed by ``_run``'s ``finally`` block on thread exit.
        If the join times out the WAV header is repaired by ``_finalize`` before
        reading, so the file is still recoverable.
        """
        self._stopped.set()
        close = getattr(self._source, "close", None)
        if callable(close):
            close()
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            _log.warning(
                "writer thread did not stop in time path=%s; WAV header will be repaired on read",
                self._writer,
            )

    def recorded_s(self) -> float:
        """Return recorded audio duration in seconds."""
        return self._writer.data_bytes / (16000 * 2)


class MeetingRecorder:
    """Start/stop/pause/resume a meeting and finalize it on stop."""

    def __init__(
        self,
        store: MeetingStore,
        transcriber: MeetingTranscriber,
        summarizer: MeetingSummarizer,
        *,
        near_branch_factory: Callable[[], object],
        far_source_factory: Callable[[], SystemAudioSource],
        keep_audio: bool,
        keep: int = 20,
        on_event: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        """Initialize the recorder with injected dependencies.

        Args:
            store: Meeting store for folder/manifest management.
            transcriber: Transcribes WAVs after recording.
            summarizer: Summarizes the transcript into minutes.
            near_branch_factory: Factory returning a FrameSource-like object for mic.
            far_source_factory: Factory returning a SystemAudioSource for far end.
            keep_audio: If True, keep the WAV files after finalizing.
            keep: Number of recent meetings to retain (older are pruned).
            on_event: Optional callback invoked with ``status()`` on state changes.
        """
        self._store = store
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._near_factory = near_branch_factory
        self._far_factory = far_source_factory
        self._keep_audio = keep_audio
        self._keep = keep
        self._on_event = on_event
        self._lock = threading.Lock()
        self._active: _Active | None = None

    def _emit(self) -> None:
        if self._on_event is not None:
            self._on_event(self.status())

    def start(self, title: str) -> str:
        """Begin capture; degrade to mic-only if the far end can't start.

        Args:
            title: Human-readable meeting title.

        Returns:
            Spoken-friendly acknowledgment string.
        """
        with self._lock:
            if self._active is not None:
                return "A meeting is already recording. Say 'stop recording' to finish it first."
            paths = self._store.create(title)
            started = datetime.now()
            near_writer = _StreamWriter(self._near_factory(), paths.near_wav)
            far_writer: _StreamWriter | None = None
            mic_only = False
            far_reason = ""
            try:
                far_writer = _StreamWriter(self._far_factory(), paths.far_wav)
            except Exception as exc:
                mic_only = True
                far_reason = str(exc)
                _log.warning("far-end unavailable, mic-only: %s", far_reason)
            self._active = _Active(
                paths=paths,
                title=title or "Meeting",
                started=started,
                near=near_writer,
                far=far_writer,
                mic_only=mic_only,
            )
            near_writer.start()
            if far_writer is not None:
                far_writer.start()
            self._write_manifest("recording")
            _log.info("meeting start id=%s mic_only=%s", paths.id, mic_only)
        self._emit()
        if mic_only:
            return (
                "Recording the meeting — but I can only hear your side (mic-only); "
                "the other participants' audio isn't being captured."
            )
        return "Recording the meeting."

    def pause(self) -> str:
        """Pause the active recording.

        Returns:
            Spoken-friendly status string.
        """
        with self._lock:
            if self._active is None:
                return "No meeting is recording."
            if self._active.paused:
                return "The meeting is already paused."
            self._active.paused = True
            self._active.pauses.append({"at": datetime.now().isoformat()})
            self._active.near.pause()
            if self._active.far is not None:
                self._active.far.pause()
            self._write_manifest("paused")
        self._emit()
        return "Paused the recording."

    def resume(self) -> str:
        """Resume a paused recording.

        Returns:
            Spoken-friendly status string.
        """
        with self._lock:
            if self._active is None:
                return "No meeting is recording."
            if not self._active.paused:
                return "The meeting isn't paused."
            self._active.paused = False
            if self._active.pauses:
                self._active.pauses[-1]["resumed_at"] = datetime.now().isoformat()
            self._active.near.resume()
            if self._active.far is not None:
                self._active.far.resume()
            self._write_manifest("recording")
        self._emit()
        return "Resumed the recording."

    def stop(self) -> str:
        """Stop capture, finalize (transcribe→summarize→write), and return path.

        Returns:
            Spoken-friendly string with the saved path, or a no-meeting message.
        """
        with self._lock:
            active = self._active
            if active is None:
                return "No meeting is recording."
            self._active = None
        active.near.stop()
        if active.far is not None:
            active.far.stop()
        self._finalize(active)
        self._store.prune(self._keep)
        self._emit()
        return f"Saved the meeting minutes to {active.paths.dir}."

    def _far_stream_status(self, active: _Active) -> str:
        """Compute the far_stream status string for the manifest.

        Args:
            active: The meeting whose far stream to assess.

        Returns:
            ``"ok"``, ``"interrupted"``, or ``"unavailable"``.
        """
        if active.mic_only:
            return "unavailable"
        if active.far is not None and active.far.failed:
            return "interrupted"
        return "ok"

    def _finalize(self, active: _Active) -> None:
        """Transcribe → summarize → write files; repair headers first.

        Args:
            active: The completed meeting's mutable state.
        """
        self._write_manifest_for(active, "transcribing")
        self._emit()
        near_p = Path(active.paths.near_wav)
        far_p = Path(active.paths.far_wav)
        if near_p.exists():
            repair_header(active.paths.near_wav)
        if active.far is not None and far_p.exists():
            repair_header(active.paths.far_wav)
        far_wav = active.paths.far_wav if (active.far is not None and not active.mic_only) else None
        transcript = self._transcriber.build(
            active.paths.near_wav,
            far_wav,
            mic_only=active.mic_only,
        )
        Path(active.paths.transcript_md).write_text(transcript, encoding="utf-8")
        self._write_manifest_for(active, "summarizing")
        self._emit()
        duration = _fmt_duration((datetime.now() - active.started).total_seconds())
        try:
            minutes = self._summarizer.summarize(
                transcript,
                title=active.title,
                date=active.started.strftime("%Y-%m-%d"),
                duration=duration,
                mic_only=active.mic_only,
            )
        except Exception as exc:
            _log.exception("summary failed")
            minutes = (
                f"# {active.title}\n\n_Summary unavailable ({exc}). The transcript is "
                "saved — run 'summarize the last meeting' to rebuild minutes._\n"
            )
        Path(active.paths.minutes_md).write_text(minutes, encoding="utf-8")
        if not self._keep_audio:
            for p in (near_p, far_p):
                if p.exists():
                    p.unlink()
        self._write_manifest_for(active, "done")
        _log.info("meeting done id=%s", active.paths.id)

    def status(self) -> dict[str, object]:
        """Return a snapshot of the current recording state.

        Returns:
            Dict with keys: active, paused, mic_only, elapsed_s, recorded_s, title, state.
        """
        a = self._active
        if a is None:
            return {
                "active": False,
                "paused": False,
                "mic_only": False,
                "elapsed_s": 0.0,
                "recorded_s": 0.0,
                "title": "",
                "state": "idle",
            }
        elapsed = (datetime.now() - a.started).total_seconds()
        return {
            "active": True,
            "paused": a.paused,
            "mic_only": a.mic_only,
            "elapsed_s": round(elapsed, 1),
            "recorded_s": round(a.near.recorded_s(), 1),
            "title": a.title,
            "state": "paused" if a.paused else "recording",
        }

    def list_recent(self) -> list[dict[str, object]]:
        """Return saved meetings' manifests, newest first (delegates to the store)."""
        return self._store.list_recent()

    def finalize_interrupted(self) -> list[str]:
        """On startup, finalize any meeting left mid-flight from on-disk WAVs.

        Returns:
            List of meeting IDs that were successfully recovered.
        """
        recovered: list[str] = []
        for meeting_id in self._store.find_interrupted():
            try:
                self._recover_one(meeting_id)
                recovered.append(meeting_id)
            except Exception:
                _log.exception("recovery failed id=%s", meeting_id)
        return recovered

    def resummarize(self, meeting_id: str | None) -> str:
        """Re-run the summarizer on a past meeting and overwrite minutes.md.

        Reads transcript.md if present; otherwise rebuilds it from the WAVs.
        Never raises — returns a friendly error string on failure.

        Args:
            meeting_id: The meeting folder name, or None to use the most recent.

        Returns:
            A friendly confirmation or error string.
        """
        try:
            if meeting_id is None:
                recent = self._store.list_recent()
                if not recent:
                    return "No meetings found to re-summarize."
                meeting_id = str(recent[0].get("id", ""))
                if not meeting_id:
                    return "No meetings found to re-summarize."
            paths = self._store._paths(meeting_id)
            manifest = self._store.read_manifest(paths.dir)
            mic_only = bool(manifest.get("mic_only", False))

            transcript_path = Path(paths.transcript_md)
            if transcript_path.exists():
                transcript = transcript_path.read_text(encoding="utf-8")
            else:
                # Rebuild from WAVs
                near_p = Path(paths.near_wav)
                if near_p.exists():
                    repair_header(paths.near_wav)
                far_wav: str | None = None
                far_p = Path(paths.far_wav)
                if not mic_only and far_p.exists():
                    repair_header(paths.far_wav)
                    far_wav = paths.far_wav
                transcript = self._transcriber.build(paths.near_wav, far_wav, mic_only=mic_only)
                transcript_path.write_text(transcript, encoding="utf-8")

            title = str(manifest.get("title", "Meeting"))
            date = str(manifest.get("started_at", ""))[:10]
            minutes = self._summarizer.summarize(
                transcript,
                title=title,
                date=date,
                duration="rebuilt",
                mic_only=mic_only,
            )
            Path(paths.minutes_md).write_text(minutes, encoding="utf-8")
            _log.info("meeting resummarized id=%s", meeting_id)
            return f"Rebuilt the minutes for {meeting_id}."
        except Exception as exc:
            _log.exception("resummarize failed id=%s", meeting_id)
            return f"Could not re-summarize the meeting: {exc}"

    def _recover_one(self, meeting_id: str) -> None:
        paths = self._store._paths(meeting_id)
        manifest = self._store.read_manifest(paths.dir)
        mic_only = bool(manifest.get("mic_only", False))
        near_p = Path(paths.near_wav)
        if near_p.exists():
            repair_header(paths.near_wav)
        far: str | None = None
        far_p = Path(paths.far_wav)
        if not mic_only and far_p.exists():
            repair_header(paths.far_wav)
            far = paths.far_wav
        transcript = self._transcriber.build(paths.near_wav, far, mic_only=mic_only)
        Path(paths.transcript_md).write_text(transcript, encoding="utf-8")
        minutes = self._summarizer.summarize(
            transcript,
            title=str(manifest.get("title", "Meeting")),
            date=str(manifest.get("started_at", ""))[:10],
            duration="recovered",
            mic_only=mic_only,
        )
        Path(paths.minutes_md).write_text(minutes, encoding="utf-8")
        data = dict(manifest)
        data["state"] = "done"
        self._store.write_manifest(paths, data)
        _log.info("meeting recovered id=%s", meeting_id)

    def _write_manifest(self, state: str) -> None:
        assert self._active is not None
        self._write_manifest_for(self._active, state)

    def _write_manifest_for(self, active: _Active, state: str) -> None:
        self._store.write_manifest(
            active.paths,
            {
                "id": active.paths.id,
                "title": active.title,
                "started_at": active.started.isoformat(),
                "state": state,
                "mic_only": active.mic_only,
                "far_stream": {"status": self._far_stream_status(active)},
                "pauses": active.pauses,
            },
        )


class _Active:
    """Mutable state for the one in-flight meeting."""

    def __init__(
        self,
        *,
        paths: MeetingPaths,
        title: str,
        started: datetime,
        near: _StreamWriter,
        far: _StreamWriter | None,
        mic_only: bool,
    ) -> None:
        self.paths = paths
        self.title = title
        self.started = started
        self.near = near
        self.far = far
        self.mic_only = mic_only
        self.paused = False
        self.pauses: list[dict[str, str]] = []


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
