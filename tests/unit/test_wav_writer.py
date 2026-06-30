from __future__ import annotations

import numpy as np

from autobot.meeting.wav import WavWriter, read_wav, repair_header


def test_write_read_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "near.wav"
    w = WavWriter(str(p))
    w.append(np.full(512, 0.5, dtype=np.float32))
    w.append(np.full(512, -0.5, dtype=np.float32))
    w.close()
    audio = read_wav(str(p))
    assert audio.shape == (1024,) and audio.dtype == np.float32
    assert abs(audio[0] - 0.5) < 1e-3 and abs(audio[600] + 0.5) < 1e-3


def test_repair_header_after_crash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "far.wav"
    w = WavWriter(str(p))
    w.append(np.zeros(512, dtype=np.float32))
    # Simulate a crash: bytes are on disk but close() never patched the sizes.
    raw = bytearray(p.read_bytes())
    raw[4:8] = b"\x00\x00\x00\x00"  # zero RIFF size
    raw[40:44] = b"\x00\x00\x00\x00"  # zero data size
    p.write_bytes(bytes(raw))
    n = repair_header(str(p))
    assert n == 512
    assert read_wav(str(p)).shape == (512,)
