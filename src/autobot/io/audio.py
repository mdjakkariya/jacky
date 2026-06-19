"""Push-to-talk microphone capture (Phase 0 :class:`AudioSource`).

Press Enter to start recording, Enter again to stop. The output contract — a
mono ``float32`` clip at the configured sample rate — is the same one the
Phase 2 wake-word + VAD source will satisfy, so :mod:`autobot.stt` never changes.

``sounddevice`` is imported lazily inside :meth:`record_clip` so that importing
this module (e.g. during tests) does not require PortAudio to be present.
"""

from __future__ import annotations

import sys
import threading

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip


class PushToTalkRecorder:
    """Captures one utterance per :meth:`record_clip`, gated by the Enter key."""

    def __init__(self, settings: Settings) -> None:
        self._sample_rate = settings.sample_rate
        self._channels = settings.channels

    def record_clip(self) -> AudioClip:
        """Record from the default mic until the user presses Enter twice.

        Returns:
            A 1-D ``float32`` array of mono samples at the configured rate; empty
            if nothing was captured.
        """
        import sounddevice as sd

        input("\n[mic] Press Enter to START recording…")

        frames: list[AudioClip] = []
        stop = threading.Event()

        def callback(indata: AudioClip, _frames: int, _time: object, status: object) -> None:
            if status:
                print(f"[mic] {status}", file=sys.stderr)
            frames.append(indata.copy())

        def wait_for_stop() -> None:
            input()
            stop.set()

        stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="float32",
            callback=callback,
        )

        with stream:
            print("[mic] Recording… press Enter to STOP.")
            # Wait for the second Enter on a worker thread so the audio callback
            # keeps draining the device.
            waiter = threading.Thread(target=wait_for_stop)
            waiter.start()
            stop.wait()
            waiter.join()

        if not frames:
            return np.zeros(0, dtype=np.float32)

        audio: AudioClip = np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)
        seconds = len(audio) / self._sample_rate
        print(f"[mic] Captured {seconds:.1f}s of audio.")
        return audio
