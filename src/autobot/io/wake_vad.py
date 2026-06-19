"""Thin wrappers around the wake-word and voice-activity models.

Both heavy runtimes (openWakeWord, silero-vad) are imported lazily inside
``__init__`` and surfaced behind tiny ``Protocol``s, so the rest of the listening
code — and the tests — depend only on two callables:

* :class:`WakeDetector` — ``score(frame_int16) -> float`` (wake probability)
* :class:`VoiceActivity` — ``speech_prob(frame_float32) -> float``

This keeps the real-time loop in :mod:`autobot.io.listening` fully testable with
fakes, and confines the optional ``wake`` dependencies to this module.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from autobot.core.types import AudioClip, Int16Frame


@runtime_checkable
class WakeDetector(Protocol):
    """Scores how strongly a frame matches the configured wake phrase."""

    def score(self, frame_int16: Int16Frame) -> float:
        """Return the wake-word probability in ``[0, 1]`` for one frame."""
        ...

    def reset(self) -> None:
        """Clear any internal buffering between detections."""
        ...


@runtime_checkable
class VoiceActivity(Protocol):
    """Estimates the probability that a frame contains speech."""

    def speech_prob(self, frame: AudioClip) -> float:
        """Return the speech probability in ``[0, 1]`` for one 16 kHz frame."""
        ...

    def reset(self) -> None:
        """Clear recurrent state between utterances."""
        ...


class OpenWakeWord:
    """Wake-word detector backed by openWakeWord (ONNX)."""

    def __init__(self, model_name: str) -> None:
        # Lazy import: only needed in hands-free mode, only installed via the
        # optional ``wake`` extra.
        from openwakeword.model import Model

        self._model_name = model_name
        self._model = Model(wakeword_models=[model_name])

    def score(self, frame_int16: Int16Frame) -> float:
        """Run the model on one frame and return the configured phrase's score."""
        scores = self._model.predict(frame_int16)
        return float(scores.get(self._model_name, 0.0))

    def reset(self) -> None:
        """Clear openWakeWord's internal prediction buffer."""
        self._model.reset()


class SileroVad:
    """Voice-activity detector backed by silero-vad."""

    def __init__(self) -> None:
        from silero_vad import load_silero_vad

        self._model = load_silero_vad()
        # ``torch`` is a transitive dependency of silero-vad; import it lazily too.
        import torch

        self._torch = torch

    def speech_prob(self, frame: AudioClip) -> float:
        """Return the speech probability for one 512-sample 16 kHz frame."""
        tensor = self._torch.from_numpy(np.ascontiguousarray(frame))
        return float(self._model(tensor, 16_000).item())

    def reset(self) -> None:
        """Reset silero's recurrent hidden state between utterances."""
        self._model.reset_states()
