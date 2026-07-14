"""Tests for the debug-bundle assembler (pure)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from autobot.cli import debug_report


def test_newest_transcript_picks_latest_by_mtime(tmp_path: Path) -> None:
    sessions = tmp_path / ".jack" / "sessions"
    sessions.mkdir(parents=True)
    old = sessions / "old.jsonl"
    old.write_text("{}", encoding="utf-8")
    new = sessions / "new.jsonl"
    new.write_text("{}", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert debug_report.newest_transcript(str(tmp_path)) == new


def test_newest_transcript_none_when_absent(tmp_path: Path) -> None:
    assert debug_report.newest_transcript(str(tmp_path)) is None


def test_cost_line_from_usage() -> None:
    usage: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "session": {
            "turns": 3,
            "in": 30,
            "out": 3260,
            "cache_read": 269005,
            "cache_write": 31736,
            "usd": 0.2487,
        },
    }
    line = debug_report.cost_line(usage)
    assert "3 turns" in line and "$0.2487" in line and "claude-sonnet-5" in line


def test_cost_line_empty_usage() -> None:
    assert "none recorded" in debug_report.cost_line({})


def test_build_bundle_has_header_transcript_cost_and_report() -> None:
    bundle = debug_report.build_bundle(
        "## Errors & warnings\n(none)",
        transcript=Path("/w/.jack/sessions/x.jsonl"),
        cost="Session usage: 1 turns",
    )
    assert "debug bundle" in bundle
    assert "x.jsonl" in bundle
    assert "Session usage: 1 turns" in bundle
    assert "## Errors & warnings" in bundle


def test_build_bundle_handles_empty_report_and_no_transcript() -> None:
    bundle = debug_report.build_bundle("", transcript=None, cost="Session usage: (none)")
    assert "daemon report unavailable" in bundle
    assert "(none found)" in bundle


def test_write_bundle_writes_to_jack_dir(tmp_path: Path) -> None:
    path = debug_report.write_bundle("hello", str(tmp_path))
    assert path == tmp_path / ".jack" / "debug-report.md"
    assert path.read_text(encoding="utf-8") == "hello"


def test_share_hint_mentions_pbcopy_and_transcript() -> None:
    hint = debug_report.share_hint(
        Path("/w/.jack/debug-report.md"), Path("/w/.jack/sessions/x.jsonl")
    )
    assert "pbcopy" in hint and "debug-report.md" in hint and "x.jsonl" in hint
