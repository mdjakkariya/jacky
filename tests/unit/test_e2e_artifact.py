"""Artifact bundle assembly + redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyte")

from autobot.e2e.artifact import RunRecord, write_bundle


def _record(**over: object) -> RunRecord:
    base: dict[str, object] = {
        "name": "create-file",
        "task": "make hello.py",
        "criteria": "file exists",
        "autonomy": "plan",
        "strategy": "scripted",
        "provider": "ollama:qwen",
        "screen": "⏺ done\n❯ ",
        "raw": b"\x1b[0m done",
        "steps_log": [{"action": "Send"}],
        "checks": [{"check": "FileExists", "ok": True}],
        "verdict": {"pass": True},
        "daemon_log": "[coder] turn done",
        "session_jsonl": '{"type":"meta"}',
        "settings_snapshot": '{"coding_autonomy":"plan"}',
    }
    base.update(over)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_writes_all_bundle_files(tmp_path: Path) -> None:
    d = write_bundle(_record(), root=str(tmp_path))
    for name in (
        "report.md",
        "manifest.json",
        "screen.txt",
        "raw.ansi",
        "steps.jsonl",
        "daemon.log",
        "session.jsonl",
        "settings.json",
        "judge.json",
    ):
        assert (d / name).exists(), name
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["name"] == "create-file" and manifest["verdict"] == {"pass": True}
    assert "file exists" in (d / "report.md").read_text()


def test_manual_run_omits_judge_json(tmp_path: Path) -> None:
    d = write_bundle(_record(verdict=None), root=str(tmp_path))
    assert not (d / "judge.json").exists()


def test_redacts_secretish_strings(tmp_path: Path) -> None:
    d = write_bundle(
        _record(daemon_log="key sk-ant-api03-ABCDEFGHIJKLMNOP secret"), root=str(tmp_path)
    )
    assert "sk-ant-api03-ABCDEFGHIJKLMNOP" not in (d / "daemon.log").read_text()


def test_redacts_raw_ansi_transcript(tmp_path: Path) -> None:
    d = write_bundle(_record(raw=b"key sk-ant-api03-ABCDEFGHIJKLMNOP z"), root=str(tmp_path))
    assert "sk-ant-api03-ABCDEFGHIJKLMNOP" not in (d / "raw.ansi").read_text()
