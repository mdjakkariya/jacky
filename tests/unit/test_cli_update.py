"""`jack update` dispatch + the end-of-session update notice (network mocked)."""

from __future__ import annotations

import pytest

from autobot.cli import main


def test_update_command_invokes_run_update(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import autobot.update as up

    monkeypatch.setattr(up, "run_update", lambda *a, **k: "updated to jack 0.7.0")
    assert main(["update"]) == 0
    assert "updated to jack 0.7.0" in capsys.readouterr().out


def test_update_command_reports_failure_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import autobot.update as up

    def _boom(*a: object, **k: object) -> str:
        raise RuntimeError("checksum mismatch")

    monkeypatch.setattr(up, "run_update", _boom)
    assert main(["update"]) == 1
