"""Config precedence: defaults < global settings.json < workspace .jack/settings.json."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from autobot.config import Settings, set_workspace_overlay, write_settings


@pytest.fixture(autouse=True)
def _clear_overlay() -> Iterator[None]:
    set_workspace_overlay(None)
    yield
    set_workspace_overlay(None)


def test_workspace_overrides_global(tmp_path: Path) -> None:
    global_file = tmp_path / "settings.json"
    ws_file = tmp_path / ".jack" / "settings.json"
    write_settings({"llm_provider": "ollama", "coding_autonomy": "plan"}, global_file)
    write_settings({"coding_autonomy": "auto"}, ws_file)  # workspace overrides just autonomy
    s = Settings.load(global_file, workspace_settings=ws_file)
    assert s.coding_autonomy == "auto"  # from workspace
    assert s.llm_provider == "ollama"  # from global (not overridden)


def test_process_overlay_applies_to_plain_load(tmp_path: Path) -> None:
    global_file = tmp_path / "settings.json"
    ws_file = tmp_path / ".jack" / "settings.json"
    write_settings({"coding_autonomy": "plan"}, global_file)
    write_settings({"coding_autonomy": "confirm"}, ws_file)
    set_workspace_overlay(ws_file)
    # A plain load (no explicit workspace_settings) still layers the process overlay.
    assert Settings.load(global_file).coding_autonomy == "confirm"


def test_no_overlay_is_global_only(tmp_path: Path) -> None:
    global_file = tmp_path / "settings.json"
    write_settings({"coding_autonomy": "confirm"}, global_file)
    assert Settings.load(global_file).coding_autonomy == "confirm"
