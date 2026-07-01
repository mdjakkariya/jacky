from __future__ import annotations

from autobot import permissions


def test_audio_capture_status_unknown_by_default() -> None:
    assert permissions.status_of(permissions.AUDIO_CAPTURE) == permissions.UNKNOWN


def test_snapshot_includes_audio_capture() -> None:
    keys = [permissions.MICROPHONE, permissions.AUDIO_CAPTURE]
    snap = {row["key"]: row for row in permissions.snapshot(keys)}
    assert snap[permissions.AUDIO_CAPTURE]["label"] == "Audio Capture"
    assert snap[permissions.AUDIO_CAPTURE]["description"]
