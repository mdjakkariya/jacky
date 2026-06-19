"""Pure, model-free logic for the always-on listening loop.

The real-time audio path is the riskiest part of Phase 2, so the decision logic
is deliberately separated from the microphone and the ML models and lives here as
small, deterministic, fully-unit-tested pieces:

* :class:`TrailingSilenceEndpointer` — decides when an utterance has ended, from a
  stream of per-frame voice-activity probabilities.
* :class:`FramePrebuffer` — a fixed-size ring buffer holding the most recent
  frames, so the moment *before* the wake word fires isn't clipped.
* :func:`float_to_int16` — sample-format conversion for the wake-word model.

Everything here is synchronous and side-effect-free, so tests feed synthetic
sequences and assert the decisions without any audio hardware.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from autobot.core.types import AudioClip, Int16Frame


def float_to_int16(frame: AudioClip) -> Int16Frame:
    """Convert a ``float32`` PCM frame in ``[-1, 1]`` to ``int16`` samples.

    openWakeWord expects 16-bit integer samples; clipping guards against values
    slightly outside the range.
    """
    clipped = np.clip(frame, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


class TrailingSilenceEndpointer:
    """Detect end-of-speech: start on sustained speech, stop on trailing silence.

    A frame counts as speech when its VAD probability is at least
    ``speech_threshold``. The utterance is considered *started* after
    ``start_frames`` speech frames, and *finished* once ``end_silence_frames``
    consecutive non-speech frames follow. This avoids both firing on a brief
    blip and cutting the user off mid-pause.
    """

    def __init__(
        self,
        speech_threshold: float,
        start_frames: int,
        end_silence_frames: int,
    ) -> None:
        self._speech_threshold = speech_threshold
        self._start_frames = max(1, start_frames)
        self._end_silence_frames = max(1, end_silence_frames)
        self._speech_run = 0
        self._silence_run = 0
        self._started = False

    @property
    def started(self) -> bool:
        """Whether sustained speech has begun."""
        return self._started

    @property
    def finished(self) -> bool:
        """Whether speech started and has since been followed by enough silence."""
        return self._started and self._silence_run >= self._end_silence_frames

    def update(self, speech_prob: float) -> None:
        """Feed one frame's voice-activity probability and update the state."""
        is_speech = speech_prob >= self._speech_threshold
        if not self._started:
            self._speech_run = self._speech_run + 1 if is_speech else 0
            if self._speech_run >= self._start_frames:
                self._started = True
                self._silence_run = 0
            return
        self._silence_run = 0 if is_speech else self._silence_run + 1


class FramePrebuffer:
    """A ring buffer of the most recent audio frames (for wake-word pre-roll)."""

    def __init__(self, max_frames: int) -> None:
        self._frames: deque[AudioClip] = deque(maxlen=max(1, max_frames))

    def push(self, frame: AudioClip) -> None:
        """Add a frame, evicting the oldest once full."""
        self._frames.append(frame)

    def drain(self) -> list[AudioClip]:
        """Return the buffered frames (oldest first) and clear the buffer."""
        frames = list(self._frames)
        self._frames.clear()
        return frames
