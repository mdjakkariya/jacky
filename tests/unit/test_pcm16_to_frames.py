from __future__ import annotations

import numpy as np

from autobot.io.system_audio_mac import pcm16_to_frames


def test_splits_into_full_frames_and_keeps_leftover() -> None:
    samples = np.array([0, 32767, -32768, 16384] * 200, dtype=np.int16)  # 800 samples
    frames, leftover = pcm16_to_frames(samples.tobytes(), b"", frame_samples=512)
    assert len(frames) == 1  # 800 // 512 = 1 full frame
    assert frames[0].dtype == np.float32 and frames[0].shape == (512,)
    assert abs(frames[0][1] - (32767 / 32768.0)) < 1e-4
    assert len(leftover) == (800 - 512) * 2  # remaining bytes carried over


def test_leftover_is_prepended() -> None:
    half = np.zeros(256, dtype=np.int16).tobytes()
    frames, leftover = pcm16_to_frames(half, half, frame_samples=512)  # 256 + 256 = 512
    assert len(frames) == 1 and leftover == b""
