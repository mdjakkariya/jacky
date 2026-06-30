"""Far-end (system output) capture via the native ``autobot-syscap`` Core Audio tap.

Spawns the bundled, signed Swift sidecar and reads little-endian int16 PCM frames
off its stdout. Lazy/guarded like :mod:`autobot.io.aec_mac`: any failure (helper
missing/unsigned, permission denied, OS too old, no audio) raises so the caller
degrades to mic-only rather than crashing. Validated manually on hardware — the
exact tap routing and the Audio-Capture prompt can't be exercised in CI.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import numpy as np

from autobot.core.types import AudioClip
from autobot.logging_setup import get_logger

_log = get_logger("meeting")

_INT16_SCALE = 32768.0


def pcm16_to_frames(
    data: bytes, leftover: bytes, frame_samples: int = 512
) -> tuple[list[AudioClip], bytes]:
    """Convert raw int16-LE bytes (+ any prior leftover) into full float32 frames.

    Returns the list of complete ``frame_samples``-length frames and the trailing
    bytes that didn't fill a frame, to be prepended next call.
    """
    buf = leftover + data
    nbytes = len(buf)
    frame_bytes = frame_samples * 2
    n_frames = nbytes // frame_bytes
    if n_frames == 0:
        return [], buf
    usable = n_frames * frame_bytes
    ints = np.frombuffer(buf[:usable], dtype="<i2").astype(np.float32) / _INT16_SCALE
    frames = [ints[i * frame_samples : (i + 1) * frame_samples] for i in range(n_frames)]
    return frames, buf[usable:]


class CoreAudioTapSource:
    """Reads far-end PCM frames from the ``autobot-syscap`` sidecar subprocess."""

    aec_active = False

    def __init__(self, binary_path: str, exclude_pid: int = 0, sample_rate: int = 16000) -> None:
        import subprocess

        self._stopped = threading.Event()
        _log.info("syscap spawning bin=%s exclude_pid=%d", binary_path, exclude_pid)
        self._proc = subprocess.Popen(
            [binary_path, "--sample-rate", str(sample_rate), "--exclude-pid", str(exclude_pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if self._proc.stdout is None:  # pragma: no cover - defensive
            raise RuntimeError("syscap: no stdout pipe")

    def frames(self) -> Iterator[AudioClip]:
        """Yield 512-sample float32 frames until the sidecar exits or :meth:`close`."""
        assert self._proc.stdout is not None
        leftover = b""
        while not self._stopped.is_set():
            chunk = self._proc.stdout.read(4096)
            if not chunk:  # EOF — sidecar exited (crash or stop)
                code = self._proc.poll()
                if code not in (0, None):
                    stderr_text = (
                        self._proc.stderr.read().decode("utf-8", "replace")
                        if self._proc.stderr
                        else ""
                    )
                    _log.warning("syscap exited code=%s err=%s", code, stderr_text.strip()[:200])
                break
            new_frames, leftover = pcm16_to_frames(chunk, leftover)
            yield from new_frames

    def close(self) -> None:
        """Stop the sidecar and release it. Idempotent."""
        self._stopped.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        _log.info("syscap closed")
