"""The pure workspace-root resolver for the engine (coder = workspace, assistant = sandbox)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.app import resolve_workspace_root


def test_coder_uses_explicit_workspace(tmp_path: Path) -> None:
    assert resolve_workspace_root(True, str(tmp_path), "/sandbox") == tmp_path.resolve()


def test_coder_defaults_to_cwd_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace_root(True, None, "/sandbox") == tmp_path.resolve()


def test_assistant_uses_sandbox(tmp_path: Path) -> None:
    sandbox = tmp_path / "sb"
    assert resolve_workspace_root(False, None, str(sandbox)) == sandbox.expanduser().resolve()
