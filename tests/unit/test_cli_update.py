"""`jack update` dispatch + the end-of-session update notice (network mocked)."""

from __future__ import annotations

import sys

import pytest

from autobot.cli import _print_update_notice, main


def test_update_command_invokes_run_update(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import autobot.update as up

    monkeypatch.setattr(sys, "frozen", True, raising=False)  # simulate the packaged binary
    monkeypatch.setattr(up, "run_update", lambda *a, **k: "updated to jack 0.7.0")
    assert main(["update"]) == 0
    assert "updated to jack 0.7.0" in capsys.readouterr().out


def test_update_command_reports_failure_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import autobot.update as up

    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def _boom(*a: object, **k: object) -> str:
        raise RuntimeError("checksum mismatch")

    monkeypatch.setattr(up, "run_update", _boom)
    assert main(["update"]) == 1


def test_update_refused_from_source_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Not frozen (a source/dev checkout): the guard refuses so self-replace can't overwrite
    # the venv Python, and run_update is never called.
    import autobot.update as up

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    called = False

    def _spy(*a: object, **k: object) -> str:
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(up, "run_update", _spy)
    assert main(["update"]) == 1
    assert called is False
    assert "source checkout" in capsys.readouterr().err


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
