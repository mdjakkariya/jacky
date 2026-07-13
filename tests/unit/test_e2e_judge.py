"""Deterministic checks + judge prompt/parse (no LLM call)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyte")

from autobot.e2e.judge import build_judge_prompt, parse_verdict, run_checks
from autobot.e2e.scenario import Check, FileContains, FileExists, FileLacks, ScreenContains


def test_run_checks_reports_each(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('hi')")
    checks: list[Check] = [
        FileExists("a.py"),
        FileExists("missing.py"),
        FileContains("a.py", "print"),
        FileLacks("a.py", "TODO"),  # present file, needle absent → pass
        FileLacks("a.py", "print"),  # present file, needle present → fail
        FileLacks("missing.py", "x"),  # missing file → fail (proves nothing about a revert)
        ScreenContains("done"),
    ]
    res = run_checks(checks, tmp_path, "⏺ done")
    oks = [r["ok"] for r in res]
    assert oks == [True, False, True, True, False, False, True]


def test_parse_verdict_handles_fenced_json() -> None:
    v = parse_verdict(
        '```json\n{"pass": true, "confidence": 0.9, "reasoning": "ok", "ux_notes": []}\n```'
    )
    assert v["pass"] is True and v["confidence"] == 0.9


def test_parse_verdict_garbage_is_safe() -> None:
    v = parse_verdict("the model rambled with no json")
    assert v["pass"] is False and "unparseable" in v["reasoning"].lower()


def test_prompt_includes_criteria_and_screen() -> None:
    p = build_judge_prompt(
        "s", "make x", "x exists", "⏺ made x", [{"check": "FileExists", "ok": True}]
    )
    assert "x exists" in p and "⏺ made x" in p and "JSON" in p
