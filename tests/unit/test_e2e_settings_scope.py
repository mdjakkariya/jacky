"""settings_scope snapshots and restores the settings file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyte")

from autobot.e2e.settings_scope import settings_scope


def test_applies_then_restores_existing(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"coding_autonomy": "plan", "llm_model": "keep"}))
    with settings_scope({"coding_autonomy": "confirm"}, path=str(p)):
        inside = json.loads(p.read_text())
        assert inside["coding_autonomy"] == "confirm" and inside["llm_model"] == "keep"
    after = json.loads(p.read_text())
    assert after["coding_autonomy"] == "plan"  # restored


def test_removes_file_if_absent_before(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    with settings_scope({"coding_autonomy": "auto"}, path=str(p)):
        assert p.exists()
    assert not p.exists()  # restored to absent


def test_restores_on_exception(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"coding_autonomy": "plan"}))
    with pytest.raises(RuntimeError), settings_scope({"coding_autonomy": "auto"}, path=str(p)):
        raise RuntimeError("boom")
    assert json.loads(p.read_text())["coding_autonomy"] == "plan"
