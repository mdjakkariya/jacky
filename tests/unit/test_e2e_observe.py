"""The bundle observability captures — pure path -> str, best-effort, never raise."""

from __future__ import annotations

from pathlib import Path

from autobot.config import Settings
from autobot.e2e import observe


def test_settings_snapshot_is_json_with_the_key_fields() -> None:
    snap = observe.settings_snapshot(Settings(profile="coder", coding_autonomy="auto"))
    assert '"profile": "coder"' in snap
    assert '"coding_autonomy": "auto"' in snap


def test_session_jsonl_reads_the_newest_transcript(tmp_path: Path) -> None:
    sessions = tmp_path / ".jack" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "old.jsonl").write_text('{"role": "user", "content": "old"}\n')
    newest = sessions / "new.jsonl"
    newest.write_text('{"role": "user", "content": "new"}\n')
    # Make "new" the most recently modified so it wins regardless of glob order.
    import os

    os.utime(sessions / "old.jsonl", (1, 1))
    os.utime(newest, (2, 2))
    assert observe.session_jsonl(tmp_path) == '{"role": "user", "content": "new"}\n'


def test_session_jsonl_missing_dir_is_empty(tmp_path: Path) -> None:
    assert observe.session_jsonl(tmp_path) == ""  # no .jack/sessions at all


def test_session_jsonl_when_sessions_path_is_not_a_directory(tmp_path: Path) -> None:
    # `.jack` exists as a FILE, so globbing `.jack/sessions/*` raises — swallowed to "".
    (tmp_path / ".jack").write_text("not a dir")
    assert observe.session_jsonl(tmp_path) == ""


def test_session_jsonl_skips_an_unreadable_entry(tmp_path: Path) -> None:
    # A directory named like a transcript makes read_text raise (IsADirectoryError); the
    # loop skips it rather than blowing up, and returns "" when nothing readable remains.
    sessions = tmp_path / ".jack" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "not-a-file.jsonl").mkdir()
    assert observe.session_jsonl(tmp_path) == ""


def test_log_since_returns_only_the_appended_slice(tmp_path: Path) -> None:
    log = tmp_path / "autobot.log"
    log.write_text("before the run\n")
    offset = observe.log_offset(log)
    with log.open("a") as fh:
        fh.write("during the run\n")
    assert observe.log_since(log, offset) == "during the run\n"


def test_log_since_handles_rotation_by_reading_whole_file(tmp_path: Path) -> None:
    # If the file shrank below the offset (rotation), fall back to the whole current file.
    log = tmp_path / "autobot.log"
    log.write_text("fresh\n")
    assert observe.log_since(log, 10_000) == "fresh\n"


def test_log_offset_and_since_absent_file_are_zero_and_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope.log"
    assert observe.log_offset(missing) == 0
    assert observe.log_since(missing, 0) == ""
