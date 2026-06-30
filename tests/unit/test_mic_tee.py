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


def _take(branch: object, n: int) -> list[AudioClip]:
    out: list[AudioClip] = []
    for f in branch.frames():  # type: ignore[attr-defined]
        out.append(f)
        if len(out) == n:
            break
    return out
