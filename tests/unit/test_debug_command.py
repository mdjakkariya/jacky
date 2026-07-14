"""The /debug command assembles + writes a shareable session debug bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.cli import coder_commands


def _usage() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "session": {"turns": 2, "in": 1, "out": 2, "cache_read": 0, "cache_write": 0, "usd": 0.01},
    }


def test_debug_command_writes_bundle_and_prints_copy_hint(tmp_path: Path) -> None:
    (tmp_path / ".jack" / "sessions").mkdir(parents=True)
    (tmp_path / ".jack" / "sessions" / "s.jsonl").write_text("{}", encoding="utf-8")
    deps = coder_commands.Deps(
        get_report=lambda _b: "## Errors & warnings\n(none)",
        get_usage=lambda _b: _usage(),
        report_fallback=lambda: "SHOULD NOT BE USED",
    )
    msg = coder_commands.handle("/debug", "", base_url="http://x", cwd=str(tmp_path), deps=deps)
    assert isinstance(msg, str) and "debug-report.md" in msg and "pbcopy" in msg

    bundle = (tmp_path / ".jack" / "debug-report.md").read_text(encoding="utf-8")
    assert "## Errors & warnings" in bundle  # the daemon report is embedded
    assert "2 turns" in bundle and "claude-sonnet-5" in bundle  # session cost line
    assert "s.jsonl" in bundle  # transcript pointer
    assert "SHOULD NOT BE USED" not in bundle  # fallback not used when the daemon report exists


def test_debug_command_uses_log_fallback_when_daemon_report_empty(tmp_path: Path) -> None:
    deps = coder_commands.Deps(
        get_report=lambda _b: "",  # daemon unreachable
        get_usage=lambda _b: {},
        report_fallback=lambda: "LOG-BASED REPORT",
    )
    coder_commands.handle("/debug", "", base_url="http://x", cwd=str(tmp_path), deps=deps)
    bundle = (tmp_path / ".jack" / "debug-report.md").read_text(encoding="utf-8")
    assert "LOG-BASED REPORT" in bundle
    assert "none recorded" in bundle  # empty usage → friendly cost line
