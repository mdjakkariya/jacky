"""Test meeting Settings fields."""

from __future__ import annotations

import json

from autobot.config import Settings


def test_meeting_defaults() -> None:
    """Test meeting settings default values."""
    s = Settings()
    assert s.allow_meetings is False
    assert s.meetings_dir == "~/.autobot/meetings"
    assert s.meeting_keep == 20 and s.meeting_keep_audio is True
    assert s.meeting_chunk_s == 30.0 and s.meeting_overlap_s == 3.0
    assert s.meeting_diarization == "dual_stream"


def test_meeting_overlay_from_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Test overlay of meeting settings from JSON file."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"allow_meetings": True, "meeting_keep": 5}))
    s = Settings.load(p)
    assert s.allow_meetings is True and s.meeting_keep == 5
