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
import numpy.typing as npt

from autobot.core.types import AudioClip, Int16Frame
from autobot.logging_setup import get_logger

_log = get_logger("wake")


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


_OWW_WINDOW = 1280
"""openWakeWord's expected frame: 1280 samples = 80 ms at 16 kHz."""


class OpenWakeWord:
    """Wake-word detector backed by openWakeWord (ONNX).

    The mic delivers 512-sample (32 ms) frames for silero VAD, but openWakeWord
    expects 80 ms (1280-sample) windows — feeding it odd sizes degrades detection.
    So we buffer incoming frames and run :meth:`predict` on full 80 ms windows.
    """

    def __init__(self, model_name: str) -> None:
        # Lazy import: only needed in hands-free mode, only installed via the
        # optional ``wake`` extra.
        from openwakeword.model import Model

        _log.info("loading wake model=%s window=%d", model_name, _OWW_WINDOW)
        self._model_name = model_name
        self._model = Model(wakeword_models=[model_name])
        self._buffer: Int16Frame = np.empty(0, dtype=np.int16)

    def score(self, frame_int16: Int16Frame) -> float:
        """Buffer ``frame_int16`` and score any complete 80 ms windows.

        Returns the highest wake-word probability across the windows completed by
        this frame (0.0 if none completed yet).
        """
        self._buffer = np.concatenate([self._buffer, frame_int16])
        best = 0.0
        while self._buffer.shape[0] >= _OWW_WINDOW:
            window = self._buffer[:_OWW_WINDOW]
            self._buffer = self._buffer[_OWW_WINDOW:]
            scores = self._model.predict(window)
            best = max(best, float(scores.get(self._model_name, 0.0)))
        return best

    def reset(self) -> None:
        """Clear openWakeWord's internal state and our window buffer."""
        self._model.reset()
        self._buffer = np.empty(0, dtype=np.int16)


class SileroVad:
    """Voice-activity detector running silero-vad's ONNX model on onnxruntime.

    We run the vendored ``silero_vad.onnx`` directly instead of going through the
    ``silero-vad`` package, which would drag in torch + torchaudio (~half a gig once
    frozen). The model is the same; we just replicate silero's tiny state machine —
    a 512-sample frame prefixed with 64 samples of context, plus a recurrent state
    carried between frames — in numpy. This also puts the VAD on the same runtime as
    the wake word (onnxruntime). The model file ships as package data next to this
    module; see ``packaging/autobot-daemon.spec`` for the frozen-bundle inclusion.
    """

    _CONTEXT = 64
    """Samples of the previous frame the model prepends as context (16 kHz)."""

    _SR = 16_000

    def __init__(self) -> None:
        from importlib import resources

        import onnxruntime as ort

        _log.info("loading silero-vad (onnx)")
        model_path = str(resources.files("autobot.io").joinpath("silero_vad.onnx"))
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self._sr = np.array(self._SR, dtype=np.int64)
        self._state: npt.NDArray[np.float32]
        self._context: npt.NDArray[np.float32]
        self.reset()

    def speech_prob(self, frame: AudioClip) -> float:
        """Return the speech probability for one 512-sample 16 kHz frame."""
        x = np.ascontiguousarray(frame, dtype=np.float32).reshape(1, -1)
        x = np.concatenate([self._context, x], axis=1)
        out, state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._state = state
        self._context = x[:, -self._CONTEXT :]
        return float(out[0, 0])

    def reset(self) -> None:
        """Reset silero's recurrent state and frame context between utterances."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._CONTEXT), dtype=np.float32)
