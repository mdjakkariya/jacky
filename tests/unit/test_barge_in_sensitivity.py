"""Barge-in must need *sustained* speech, so echo/transients can't falsely fire.

Regression for the bug where a ~64ms echo blip during a reply was treated as the
user barging in: Jack cut its own voice and then answered the captured fragment.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from autobot.io.listening import capture_utterance


class _ScriptVad:
    """A VAD that returns a scripted sequence of speech probabilities."""

    def __init__(self, probs: Sequence[float]) -> None:
        self._probs = list(probs)
        self._i = 0

    def speech_prob(self, frame: np.ndarray) -> float:
        v = self._probs[self._i] if self._i < len(self._probs) else 0.0
        self._i += 1
        return v

    def reset(self) -> None:
        self._i = 0


def _frame_source(n: int) -> Callable[[], np.ndarray | None]:
    frames = iter([np.zeros(512, dtype=np.float32) for _ in range(n)])
    return lambda: next(frames, None)


def test_brief_blip_does_not_start_with_high_start_frames() -> None:
    # 2 voiced frames (~64 ms) then silence — an echo flicker. start_frames=8 (~256 ms).
    probs = [0.9, 0.9] + [0.0] * 6
    out = capture_utterance(
        _frame_source(len(probs)),
        _ScriptVad(probs),
        vad_threshold=0.5,
        end_silence_frames=2,
        max_frames=100,
        wait_frames=None,
        start_frames=8,
    )
    assert out is None  # the blip never counts as the user speaking


def test_sustained_speech_starts_and_captures() -> None:
    # 8 voiced frames meets the bar; trailing silence ends the utterance.
    probs = [0.9] * 8 + [0.0] * 3
    out = capture_utterance(
        _frame_source(len(probs)),
        _ScriptVad(probs),
        vad_threshold=0.5,
        end_silence_frames=2,
        max_frames=100,
        wait_frames=None,
        start_frames=8,
    )
    assert out is not None
    assert out.size > 0


def test_default_start_frames_still_sensitive() -> None:
    # With the default (2), a 2-frame utterance still starts — normal capture is
    # unaffected; only barge-in opts into the higher bar.
    probs = [0.9, 0.9] + [0.0] * 3
    out = capture_utterance(
        _frame_source(len(probs)),
        _ScriptVad(probs),
        vad_threshold=0.5,
        end_silence_frames=2,
        max_frames=100,
        wait_frames=None,
    )
    assert out is not None
