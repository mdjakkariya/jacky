from __future__ import annotations

import numpy as np

from autobot.core.types import AudioClip
from autobot.io.mic_tee import FrameTee


class _FakeSource:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames

    def frames(self):  # type: ignore[no-untyped-def]
        yield from self._frames

    def flush(self) -> None:
        pass


def test_every_branch_receives_every_frame() -> None:
    data = [np.full(512, i, dtype=np.float32) for i in range(5)]
    tee = FrameTee(_FakeSource(data))
    a = tee.branch()
    b = tee.branch()
    tee.start()
    got_a = [int(f[0]) for f in _take(a, 5)]
    got_b = [int(f[0]) for f in _take(b, 5)]
    tee.close()
    assert got_a == [0, 1, 2, 3, 4]
    assert got_b == [0, 1, 2, 3, 4]


def test_branch_started_idempotent_start_and_branch_receives_frames() -> None:
    """branch_started() creates a branch and starts the tee; calling start() again is a no-op."""
    import threading

    data = [np.full(512, i, dtype=np.float32) for i in range(4)]

    class _BlockingSource:
        """Infinite source that signals when drained."""

        def __init__(self) -> None:
            self._frames = data[:]
            self._done = threading.Event()

        def frames(self):  # type: ignore[no-untyped-def]
            yield from self._frames
            self._done.wait()

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self._done.set()

    src = _BlockingSource()
    tee = FrameTee(src)

    branch = tee.branch_started()  # creates branch + starts tee

    # A second call to start() should be idempotent (same thread object).
    thread_before = tee._thread
    tee.start()  # no-op
    assert tee._thread is thread_before, "start() must be idempotent"

    # The branch receives all frames from the source.
    got = [int(f[0]) for f in _take(branch, 4)]
    src.close()
    tee.close()
    assert got == [0, 1, 2, 3]


def _take(branch: object, n: int) -> list[AudioClip]:
    out: list[AudioClip] = []
    for f in branch.frames():  # type: ignore[attr-defined]
        out.append(f)
        if len(out) == n:
            break
    return out
