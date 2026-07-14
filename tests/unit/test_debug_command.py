"""The /debug command assembles + writes a shareable coder debug bundle (file-based)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from autobot.cli import coder_commands


def _seed_session(cwd: Path) -> None:
    sessions = cwd / ".jack" / "sessions"
    sessions.mkdir(parents=True)
    rows = [
        {"type": "msg", "message": {"role": "user", "content": "make a plan"}},
        {
            "type": "msg",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}}],
            },
        },
    ]
    (sessions / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _usage() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "session": {"turns": 2, "in": 1, "out": 2, "cache_read": 0, "cache_write": 0, "usd": 0.01},
    }


def test_debug_command_writes_bundle_and_prints_copy_hint(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    log = tmp_path / "autobot.log"
    log.write_text("2026-07-14 20:15:21 INFO    [coder] planning steps=2", encoding="utf-8")
    settings = SimpleNamespace(
        log_dir=str(tmp_path), coding_autonomy="auto", llm_provider="anthropic"
    )
    deps = coder_commands.Deps(get_usage=lambda _b: _usage(), load_settings=lambda: settings)

    msg = coder_commands.handle("/debug", "", base_url="http://x", cwd=str(tmp_path), deps=deps)
    assert isinstance(msg, str) and "debug-report.md" in msg and "pbcopy" in msg

    bundle = (tmp_path / ".jack" / "debug-report.md").read_text(encoding="utf-8")
    assert "you: make a plan" in bundle  # transcript excerpt (real content)
    assert "→ read_file(" in bundle
    assert "[coder] planning" in bundle  # coder-filtered log
    assert "claude-sonnet-5" in bundle and "autonomy auto" in bundle and "2 turns" in bundle
    assert "s.jsonl" in bundle  # transcript pointer


def test_debug_command_degrades_without_usage_or_transcript(tmp_path: Path) -> None:
    log = tmp_path / "autobot.log"
    log.write_text("2026-07-14 20:15:21 WARNING [gate] blocked something", encoding="utf-8")
    settings = SimpleNamespace(log_dir=str(tmp_path), coding_autonomy="plan", llm_provider="ollama")
    deps = coder_commands.Deps(get_usage=lambda _b: {}, load_settings=lambda: settings)

    coder_commands.handle("/debug", "", base_url="http://x", cwd=str(tmp_path), deps=deps)
    bundle = (tmp_path / ".jack" / "debug-report.md").read_text(encoding="utf-8")
    assert "no usage recorded yet" in bundle
    assert "(no transcript found)" in bundle
    assert "(ollama)" in bundle  # provider falls back to settings even without live usage
    assert "[gate] blocked something" in bundle  # a warning is still surfaced
