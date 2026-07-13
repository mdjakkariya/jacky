"""`jack update` dispatch + the end-of-session update notice (network mocked)."""

from __future__ import annotations

import pytest

from autobot.cli import _print_update_notice, main


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


def test_print_update_notice_prints_banner_when_newer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import autobot.update as up

    monkeypatch.setattr(up, "check_for_update", lambda *a, **k: "9.9.9")
    _print_update_notice()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == up.update_notice("9.9.9")


def test_print_update_notice_silent_when_up_to_date(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import autobot.update as up

    monkeypatch.setattr(up, "check_for_update", lambda *a, **k: None)
    _print_update_notice()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
