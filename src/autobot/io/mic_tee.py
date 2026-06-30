"""Fan one microphone frame stream out to multiple consumers.

The turn loop needs the mic (to hear "stop recording") and a meeting needs the
mic for the near end. Two opens of one device race, so a single owner thread
pulls the underlying ``FrameSource`` and copies each frame to every branch's
bounded queue. Branches that fall behind drop their oldest frame rather than
stall capture — the recorder's branch is drained promptly by a writer thread, so
in practice nothing is dropped.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Iterator

from autobot.core.types import AudioClip
from autobot.logging_setup import get_logger

_log = get_logger("meeting")

_QUEUE_MAX = 256  # ~8s of 32ms frames; ample headroom for a prompt consumer


class _Branch:
    """One subscriber's view of the shared frame stream."""

    def __init__(self) -> None:
        self._q: queue.Queue[AudioClip | None] = queue.Queue(maxsize=_QUEUE_MAX)

    def _offer(self, frame: AudioClip | None) -> None:
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._q.get_nowait()  # drop oldest
            self._q.put_nowait(frame)

    def frames(self) -> Iterator[AudioClip]:
        """Yield frames until the tee closes (a ``None`` sentinel ends iteration)."""
        while True:
            frame = self._q.get()
            if frame is None:
                return
            yield frame

    def flush(self) -> None:
        """Discard buffered frames (drop stale audio)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


class FrameTee:
    """Owns the mic ``FrameSource`` and fans its frames to branches."""

    def __init__(self, source: object) -> None:
        self._source = source
        self._branches: list[_Branch] = []
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def branch(self) -> _Branch:
        """Create a new subscriber branch (call before :meth:`start`)."""
        b = _Branch()
        self._branches.append(b)
        return b

    def start(self) -> None:
        """Begin pulling from the source on an owner thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="frame-tee", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for frame in self._source.frames():  # type: ignore[attr-defined]
                if self._stopped.is_set():
                    break
                for b in self._branches:
                    b._offer(frame)
        except Exception:
            _log.exception("frame tee source error")
        finally:
            for b in self._branches:
                b._offer(None)  # end every branch's iteration

    def close(self) -> None:
        """Stop the owner thread and close the underlying source. Idempotent."""
        self._stopped.set()
        close = getattr(self._source, "close", None)
        if callable(close):
            close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        for b in self._branches:
            b._offer(None)
