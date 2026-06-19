"""Tests for the pure listening logic (no audio hardware or models)."""

from __future__ import annotations

import numpy as np

from autobot.io.endpointing import (
    FramePrebuffer,
    TrailingSilenceEndpointer,
    float_to_int16,
)


def test_float_to_int16_scales_and_clips() -> None:
    frame = np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32)
    out = float_to_int16(frame)
    assert out.dtype == np.int16
    assert out[0] == 0
    assert out[1] == 32767
    assert out[2] == -32767
    assert out[3] == 32767  # clipped
    assert out[4] == -32767  # clipped


def test_endpointer_requires_sustained_speech_to_start() -> None:
    ep = TrailingSilenceEndpointer(speech_threshold=0.5, start_frames=3, end_silence_frames=2)
    ep.update(0.9)  # 1 speech frame
    ep.update(0.1)  # resets the run
    assert not ep.started
    ep.update(0.9)
    ep.update(0.9)
    ep.update(0.9)  # 3 in a row
    assert ep.started
    assert not ep.finished


def test_endpointer_finishes_after_trailing_silence() -> None:
    ep = TrailingSilenceEndpointer(speech_threshold=0.5, start_frames=1, end_silence_frames=3)
    ep.update(0.9)  # starts
    assert ep.started and not ep.finished
    ep.update(0.1)
    ep.update(0.1)
    assert not ep.finished  # only 2 silent frames
    ep.update(0.1)
    assert ep.finished  # 3 silent frames


def test_endpointer_silence_run_resets_on_speech() -> None:
    ep = TrailingSilenceEndpointer(speech_threshold=0.5, start_frames=1, end_silence_frames=2)
    ep.update(0.9)  # start
    ep.update(0.1)  # 1 silent
    ep.update(0.9)  # speech again -> reset
    ep.update(0.1)  # 1 silent
    assert not ep.finished
    ep.update(0.1)  # 2 silent
    assert ep.finished


def test_prebuffer_keeps_only_recent_frames() -> None:
    buf = FramePrebuffer(max_frames=2)
    a, b, c = (np.full(4, i, dtype=np.float32) for i in (1, 2, 3))
    buf.push(a)
    buf.push(b)
    buf.push(c)  # evicts a
    drained = buf.drain()
    assert len(drained) == 2
    assert np.array_equal(drained[0], b)
    assert np.array_equal(drained[1], c)
    assert buf.drain() == []  # cleared after draining
