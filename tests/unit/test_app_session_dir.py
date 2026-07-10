"""Session storage is per-workspace for the coder, global for the assistant."""

from __future__ import annotations

from pathlib import Path

from autobot.app import resolve_session_dir


def test_coder_sessions_are_per_workspace(tmp_path: Path) -> None:
    got = resolve_session_dir(True, str(tmp_path), "/sandbox", "~/.autobot/agent_sessions")
    assert got == str(tmp_path.resolve() / ".jack" / "sessions")


def test_assistant_sessions_are_global() -> None:
    got = resolve_session_dir(False, None, "/sandbox", "~/.autobot/agent_sessions")
    assert got == "~/.autobot/agent_sessions"
