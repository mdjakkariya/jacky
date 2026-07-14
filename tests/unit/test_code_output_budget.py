"""Tests for command-output budgeting (budget_output) — filesystem via tmp_path."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.code.output_budget import budget_output


def test_small_output_returned_inline(tmp_path: Path) -> None:
    assert budget_output("hello\nworld", cwd=tmp_path, cap=10_000) == "hello\nworld"
    assert not (tmp_path / ".jack" / "command-output").exists()


def test_large_output_spills_to_disk_and_returns_excerpt(tmp_path: Path) -> None:
    full = "\n".join(f"line {i}" for i in range(5_000))
    result = budget_output(full, cwd=tmp_path, cap=2_000)

    written = list((tmp_path / ".jack" / "command-output").glob("*.log"))
    assert len(written) == 1
    assert written[0].read_text() == full

    assert ".jack/command-output/" in result
    assert "read_file" in result or "grep" in result
    assert "line 4999" in result  # tail preserved
    assert "elided" in result.lower()
    assert len(result) < 2_000 + 500  # bounded to ~cap + the notice


def test_returned_path_is_cwd_relative(tmp_path: Path) -> None:
    result = budget_output("x" * 20_000, cwd=tmp_path, cap=1_000)
    assert str(tmp_path) not in result  # relative, not absolute
    assert ".jack/command-output/" in result
