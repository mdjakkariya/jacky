#!/usr/bin/env python3
"""Record the homepage demo's spoken reply using Jack's real Piper voice.

The marketing site (``docs/index.html``) plays ``docs/assets/jack-weather.mp3``
during the voice demo. This script regenerates that clip with the *same* Piper
voice the app uses (``settings.tts_voice``, default ``en_US-ryan-high`` — a male
voice), so the demo sounds exactly like Jack.

Usage (from the repo root)::

    uv run python scripts/record_demo_voice.py
    uv run python scripts/record_demo_voice.py "It's 24 degrees and sunny."

Prerequisites: the Piper voice model must be present (enable voice once, or run
``uv sync`` with the voice extra and let the model download). ``ffmpeg`` is used
to encode the MP3 and normalize levels; if it's missing the script leaves a
``.wav`` next to the target and tells you how to convert it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from autobot.config import Settings

DEFAULT_TEXT = "It's 24 degrees and sunny."
TARGET = Path(__file__).resolve().parent.parent / "docs" / "assets" / "jack-weather.mp3"


def main() -> int:
    """Synthesize the demo line and write it to the site's audio asset path."""
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT

    from piper import PiperVoice

    voice_path = Path(Settings.load().tts_voice).expanduser()
    if not voice_path.exists():
        print(f"Piper voice not found: {voice_path}")
        print("Enable voice in Jack once (so the model downloads), then re-run.")
        return 1

    print(f"voice: {voice_path.name}")
    print(f"text:  {text!r}")
    voice = PiperVoice.load(str(voice_path))
    chunks = list(voice.synthesize(text))
    if not chunks:
        print("synthesis produced no audio")
        return 1
    audio = np.concatenate([c.audio_int16_array for c in chunks])
    sample_rate = int(chunks[0].sample_rate)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    wav_path = TARGET.with_suffix(".wav")
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(audio.tobytes())

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"wrote {wav_path} (ffmpeg not found — convert it to {TARGET.name} yourself)")
        return 0

    dur = audio.size / sample_rate
    fade_out = max(0.0, dur - 0.06)
    af = (
        "dynaudnorm=f=200:g=5,alimiter=limit=0.95,"
        f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out:.3f}:d=0.06"
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-af",
            af,
            "-ar",
            "44100",
            "-ac",
            "1",
            "-b:a",
            "96k",
            str(TARGET),
        ],
        check=True,
    )
    wav_path.unlink(missing_ok=True)
    print(f"wrote {TARGET}  ({dur:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
