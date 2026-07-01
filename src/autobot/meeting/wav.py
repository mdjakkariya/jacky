"""Crash-safe incremental 16 kHz mono int16 WAV writing.

Writes a placeholder 44-byte header up front, appends frames as they arrive, and
patches the size fields on :meth:`close`. If the process dies before close, the
sizes stay zero — :func:`repair_header` rebuilds them from the file length, so a
hard crash still yields a transcribable file (design §5.5).
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from autobot.core.types import AudioClip

_SAMPLE_RATE = 16000
_HEADER_BYTES = 44
_INT16_SCALE = 32767.0


def _header(data_len: int) -> bytes:
    """Build a canonical 44-byte PCM WAV header for ``data_len`` data bytes."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        _SAMPLE_RATE,
        _SAMPLE_RATE * 2,
        2,
        16,
        b"data",
        data_len,
    )


class WavWriter:
    """Appends float32 frames to a WAV, keeping the header repairable on crash."""

    def __init__(self, path: str) -> None:
        self._f = Path(path).open("wb")  # noqa: SIM115 - long-lived, closed in close()
        self._f.write(_header(0))
        self.data_bytes = 0

    def append(self, frame: AudioClip) -> None:
        """Write one float32 frame as int16-LE PCM."""
        clipped = np.clip(frame, -1.0, 1.0)
        pcm = (clipped * _INT16_SCALE).astype("<i2").tobytes()
        self._f.write(pcm)
        self._f.flush()
        self.data_bytes += len(pcm)

    def close(self) -> None:
        """Patch the size fields and close. Idempotent."""
        if self._f.closed:
            return
        self._f.seek(0)
        self._f.write(_header(self.data_bytes))
        self._f.close()


def repair_header(path: str) -> int:
    """Rebuild a WAV's RIFF/data sizes from the file length; return sample count."""
    p = Path(path)
    size = p.stat().st_size
    data_len = max(0, size - _HEADER_BYTES)
    with p.open("r+b") as f:
        f.seek(0)
        f.write(_header(data_len))
    return data_len // 2


def read_wav(path: str) -> AudioClip:
    """Read a 16 kHz mono int16 WAV back into a float32 array (test/finalize helper)."""
    with Path(path).open("rb") as f:
        f.seek(_HEADER_BYTES)
        raw = f.read()
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
