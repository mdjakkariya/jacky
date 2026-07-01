#!/usr/bin/env python3
"""On-device validation for meeting audio capture (Task 0 de-risk).

Drives the SAME production code paths the daemon uses — `CoreAudioTapSource`
(the native ``autobot-syscap`` sidecar) and `MicFrameSource` — and reports
whether real, non-silent audio is captured. Nothing here is a mock: it spawns
the real sidecar and opens the real microphone.

Run it while music / a video / a call is playing so there's system audio to tap.

    uv run python scripts/validate_meeting.py                 # far-end tap (default, 15s)
    uv run python scripts/validate_meeting.py --seconds 20
    uv run python scripts/validate_meeting.py --mic           # near-end microphone only
    uv run python scripts/validate_meeting.py --both          # mic + tap AT ONCE (coexistence test)

The far-end test answers the one genuine unknown: does the unsigned sidecar
capture system audio on this machine, or does macOS's Audio-Capture TCC gate
require a signed binary? The `--both` test answers the flagged hardware risk:
can the meeting's mic stream and a second input coexist on this device?
"""

from __future__ import annotations

import argparse
import math
import threading
import time
from pathlib import Path

import numpy as np

from autobot.config import Settings
from autobot.io.system_audio_mac import CoreAudioTapSource
from autobot.meeting.wav import WavWriter

_SR = 16000
_SILENCE_RMS = 1e-4  # ~ -80 dBFS; below this the stream is effectively silent


def _find_binary() -> str | None:
    """Locate the sidecar: the seeded copy first, else the dev build output."""
    for p in (
        Path("~/.autobot/bin/autobot-syscap").expanduser(),
        Path("autobot-syscap/.build/release/autobot-syscap"),
    ):
        if p.exists():
            return str(p)
    return None


def _dbfs(amplitude: float) -> str:
    return "-inf" if amplitude <= 1e-9 else f"{20 * math.log10(amplitude):.1f} dBFS"


class _Stats:
    """Accumulates peak/RMS over frames written to a WAV."""

    def __init__(self, path: str) -> None:
        self.writer = WavWriter(path)
        self.path = path
        self.peak = 0.0
        self._sumsq = 0.0
        self.samples = 0

    def add(self, frame: np.ndarray) -> None:
        self.writer.append(frame)
        if frame.size:
            self.peak = max(self.peak, float(np.abs(frame).max()))
            self._sumsq += float(np.square(frame).sum())
            self.samples += frame.size

    def close(self) -> None:
        self.writer.close()

    @property
    def rms(self) -> float:
        return math.sqrt(self._sumsq / self.samples) if self.samples else 0.0

    @property
    def seconds(self) -> float:
        return self.samples / _SR

    def report(self, label: str) -> None:
        print(
            f"  {label}: {self.samples} samples ({self.seconds:.1f}s), "
            f"peak={_dbfs(self.peak)}, rms={_dbfs(self.rms)}  → {self.path}"
        )


def _drain(source: object, stats: _Stats, err_box: list[str]) -> None:
    """Consume a frame source into stats until it ends; record any error."""
    try:
        for frame in source.frames():  # type: ignore[attr-defined]
            stats.add(frame)
    except Exception as exc:  # RuntimeError from the sidecar on unexpected exit
        err_box.append(str(exc))


def _read_stderr(source: CoreAudioTapSource) -> str:
    """Best-effort read of the sidecar's stderr diagnostics (validation only)."""
    try:
        proc = source._proc  # validation tool inspecting the real capture path
        if proc.stderr:
            return proc.stderr.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""
    return ""


def validate_far(seconds: float, out: Path) -> int:
    """Capture system audio via the sidecar; verdict on whether it's real."""
    binary = _find_binary()
    if not binary:
        print(
            "❌ autobot-syscap not found. Build it first:\n"
            "     make build-syscap   (or: cd autobot-syscap && swift build -c release)"
        )
        return 2
    far = str(out / "far.wav")
    print(f"▶ FAR-END (system audio) via {binary}")
    print(f"  ▸ Play audio now (music / video / a call). Capturing {seconds:.0f}s…")
    try:
        source = CoreAudioTapSource(binary, exclude_pid=0, sample_rate=_SR)
    except Exception as exc:
        print(f"❌ could not spawn the sidecar: {exc}")
        return 1

    stats = _Stats(far)
    errs: list[str] = []
    timer = threading.Timer(seconds, source.close)
    timer.start()
    _drain(source, stats, errs)
    timer.cancel()
    source.close()
    stats.close()
    err = errs[0] if errs else _read_stderr(source)

    print()
    stats.report("far")
    if err:
        print(f"  sidecar said: {err[:600]}")
    return _verdict(stats, far, err)


def _verdict(stats: _Stats, path: str, err: str) -> int:
    if stats.seconds < 0.5:
        print(
            "❌ No audio captured. If the sidecar errored above, unsigned capture is "
            "likely blocked by the macOS Audio-Capture TCC gate — a Developer-ID "
            "signature + entitlement (packaging/syscap.entitlements) is then required.\n"
            "   If there was no error, make sure audio was actually playing and re-run."
        )
        return 1
    if stats.rms < _SILENCE_RMS:
        print(
            "⚠️  Got a stream but it's ~silent. Either nothing was playing, or unsigned "
            "capture yields silence on this machine (→ signing needed).\n"
            f"   Listen:  afplay {path}"
        )
        return 3
    print(f"✅ Captured REAL system audio (unsigned works here!). Listen:  afplay {path}")
    return 0


def validate_mic(seconds: float, out: Path) -> int:
    """Capture the microphone via the production MicFrameSource."""
    from autobot.io.listening import MicFrameSource

    near = str(out / "near.wav")
    print(f"▶ NEAR-END (microphone). Speak for {seconds:.0f}s…")
    source = MicFrameSource(Settings.load())
    stats = _Stats(near)
    errs: list[str] = []
    timer = threading.Timer(seconds, source.close)
    timer.start()
    _drain(source, stats, errs)
    timer.cancel()
    source.close()
    stats.close()
    print()
    stats.report("near")
    if errs:
        print(f"  mic error: {errs[0][:400]}")
    if stats.rms < _SILENCE_RMS:
        print(
            "⚠️  Mic stream ~silent — grant Microphone permission to your terminal, or "
            f"speak louder, and re-run.  Listen:  afplay {near}"
        )
        return 3
    print(f"✅ Microphone captured.  Listen:  afplay {near}")
    return 0


def validate_both(seconds: float, out: Path) -> int:
    """Open the mic AND the system-audio tap at once — the coexistence test.

    This is the flagged hardware risk: the meeting near-end opens its own mic
    stream while (in a real meeting) the turn loop also holds the mic, and the
    tap runs alongside. If macOS refuses a second input stream (or AEC holds the
    device exclusively), one side errors here.
    """
    from autobot.io.listening import MicFrameSource

    binary = _find_binary()
    if not binary:
        print("❌ autobot-syscap not found — run `make build-syscap` first.")
        return 2
    print(f"▶ BOTH streams at once (coexistence). Speak AND play audio for {seconds:.0f}s…")
    mic = MicFrameSource(Settings.load())
    try:
        tap = CoreAudioTapSource(binary, exclude_pid=0, sample_rate=_SR)
    except Exception as exc:
        print(f"❌ could not spawn the sidecar: {exc}")
        return 1

    near_stats, far_stats = _Stats(str(out / "near.wav")), _Stats(str(out / "far.wav"))
    near_err: list[str] = []
    far_err: list[str] = []
    t_near = threading.Thread(target=_drain, args=(mic, near_stats, near_err), daemon=True)
    t_far = threading.Thread(target=_drain, args=(tap, far_stats, far_err), daemon=True)
    t_near.start()
    t_far.start()
    time.sleep(seconds)
    mic.close()
    tap.close()
    t_near.join(timeout=3)
    t_far.join(timeout=3)
    near_stats.close()
    far_stats.close()

    print()
    near_stats.report("near")
    far_stats.report("far")
    if near_err:
        print(f"  mic error: {near_err[0][:400]}")
    if far_err:
        print(f"  tap error: {far_err[0][:400]}")
    near_ok = near_stats.rms >= _SILENCE_RMS
    far_ok = far_stats.rms >= _SILENCE_RMS
    if near_stats.seconds >= 0.5 and far_stats.seconds >= 0.5:
        print(
            f"✅ Both streams coexisted (mic {'audible' if near_ok else 'silent'}, "
            f"tap {'audible' if far_ok else 'silent'}). The two-input-stream design works "
            "on this device."
        )
        return 0 if (near_ok and far_ok) else 3
    print("❌ One stream produced no data while the other ran — they may not coexist here.")
    return 1


def main() -> int:
    """Parse args and run the requested validation stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15.0, help="capture duration")
    parser.add_argument("--out", default="~/.autobot/validation", help="output dir for WAVs")
    parser.add_argument("--mic", action="store_true", help="near-end microphone only")
    parser.add_argument("--both", action="store_true", help="mic + tap at once (coexistence)")
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    if args.both:
        return validate_both(args.seconds, out)
    if args.mic:
        return validate_mic(args.seconds, out)
    return validate_far(args.seconds, out)


if __name__ == "__main__":
    raise SystemExit(main())
