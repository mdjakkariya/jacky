"""The `jack` entry point resolves the workspace and routes `jack restart`."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli import main as cli_main
from autobot.cli import resolve_workspace


def test_resolve_workspace_prefers_arg(tmp_path: Path) -> None:
    other = tmp_path / "other"
    assert resolve_workspace(tmp_path, str(other)) == other.resolve()
    assert resolve_workspace(tmp_path, None) == tmp_path.resolve()


def test_jack_restart_calls_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = {"stopped": False}

    def fake_stop(**_k: object) -> bool:
        called["stopped"] = True
        return True

    monkeypatch.setattr("autobot.cli.stop_daemon", fake_stop)
    rc = cli_main(["restart"])
    assert rc == 0 and called["stopped"] is True
    assert "stopped" in capsys.readouterr().out.lower()
