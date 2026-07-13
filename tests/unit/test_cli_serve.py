"""`jack serve` drives the daemon; the frozen build re-execs itself instead of `python -m`."""

from __future__ import annotations

import sys

import pytest

from autobot.cli import client, main


def test_serve_delegates_to_daemon_main(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    import autobot.daemon.__main__ as dmain

    monkeypatch.setattr(dmain, "main", lambda argv=None: seen.append(list(argv or [])))
    assert main(["serve", "--profile", "coder", "--port", "8791"]) == 0
    assert seen == [["--profile", "coder", "--port", "8791"]]


def test_daemon_argv_uses_python_module_when_not_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    argv = client._daemon_argv(8791, "/tmp/ws")
    assert argv == [
        "/usr/bin/python3",
        "-m",
        "autobot.daemon",
        "--profile",
        "coder",
        "--port",
        "8791",
        "--workspace",
        "/tmp/ws",
    ]


def test_daemon_argv_reexecs_self_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/jack")
    argv = client._daemon_argv(8791, "/tmp/ws")
    assert argv == [
        "/opt/jack",
        "serve",
        "--profile",
        "coder",
        "--port",
        "8791",
        "--workspace",
        "/tmp/ws",
    ]
