"""Tests for the coder debug-bundle assembler (pure)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from autobot.cli import debug_report


def _write_transcript(path: Path) -> None:
    rows = [
        {"type": "meta", "model": "claude-sonnet-5"},
        {"type": "msg", "message": {"role": "user", "content": "help me test this project"}},
        {
            "type": "msg",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running the suite."},
                    {"type": "tool_use", "name": "run_command", "input": {"command": "npx pw"}},
                ],
            },
        },
        {
            "type": "msg",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "Command timed out after 300s"},
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_newest_transcript_picks_latest_by_mtime(tmp_path: Path) -> None:
    sessions = tmp_path / ".jack" / "sessions"
    sessions.mkdir(parents=True)
    old, new = sessions / "old.jsonl", sessions / "new.jsonl"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert debug_report.newest_transcript(str(tmp_path)) == new


def test_newest_transcript_none_when_absent(tmp_path: Path) -> None:
    assert debug_report.newest_transcript(str(tmp_path)) is None


def test_transcript_excerpt_renders_conversation_and_tools(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    _write_transcript(path)
    excerpt = debug_report.transcript_excerpt(path)
    assert "you: help me test this project" in excerpt
    assert "jack: Running the suite." in excerpt
    assert "→ run_command(" in excerpt and "npx pw" in excerpt
    assert "result: Command timed out after 300s" in excerpt


def test_transcript_excerpt_missing_file() -> None:
    assert debug_report.transcript_excerpt(None) == "(no transcript found)"


def test_coder_log_tail_keeps_coder_and_warnings_drops_voice(tmp_path: Path) -> None:
    log = tmp_path / "autobot.log"
    log.write_text(
        "\n".join(
            [
                "2026-07-14 20:15:21 INFO    [coder] planning steps=3",
                "2026-07-14 20:15:21 INFO    [toggles] volume set to=30",
                "2026-07-14 20:15:21 DEBUG   [llm] cloud usage tokens=5",
                "2026-07-14 20:15:21 WARNING [web] api down; falling back",
                "2026-07-14 20:15:21 INFO    [listening] wake fired",
                '  File "x.py", line 5, in boom',
            ]
        ),
        encoding="utf-8",
    )
    tail = debug_report.coder_log_tail(log)
    assert "[coder] planning" in tail
    assert "[llm] cloud usage" in tail
    assert "[web] api down" in tail  # kept because it's a WARNING
    assert "[toggles]" not in tail and "[listening]" not in tail  # voice noise dropped


def test_context_line_with_and_without_usage() -> None:
    usage: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "session": {
            "turns": 3,
            "in": 30,
            "out": 3260,
            "cache_read": 10,
            "cache_write": 5,
            "usd": 0.25,
        },
    }
    line = debug_report.context_line(usage, "auto")
    assert "claude-sonnet-5" in line and "autonomy auto" in line
    assert "3 turns" in line and "$0.25" in line
    assert "no usage recorded yet" in debug_report.context_line({}, "plan")


def test_build_bundle_has_all_sections(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    _write_transcript(transcript)
    log = tmp_path / "autobot.log"
    log.write_text("2026-07-14 20:15:21 INFO    [coder] did a thing", encoding="utf-8")
    bundle = debug_report.build_bundle(
        transcript=transcript,
        log_path=log,
        cwd="/w",
        usage={"model": "m", "provider": "p", "session": {"turns": 1}},
        autonomy="plan",
    )
    assert "session debug bundle" in bundle
    assert "Workspace: /w" in bundle and "autonomy plan" in bundle
    assert "## Transcript (recent steps)" in bundle and "you: help me test this project" in bundle
    assert "## Recent coder log" in bundle and "[coder] did a thing" in bundle


def test_write_bundle_writes_to_jack_dir(tmp_path: Path) -> None:
    path = debug_report.write_bundle("hello", str(tmp_path))
    assert path == tmp_path / ".jack" / "debug-report.md"
    assert path.read_text(encoding="utf-8") == "hello"


def test_share_hint_mentions_pbcopy_and_transcript() -> None:
    hint = debug_report.share_hint(
        Path("/w/.jack/debug-report.md"), Path("/w/.jack/sessions/x.jsonl")
    )
    assert "pbcopy" in hint and "debug-report.md" in hint and "x.jsonl" in hint
