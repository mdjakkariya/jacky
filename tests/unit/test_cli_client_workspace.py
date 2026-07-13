"""Workspace-aware daemon control in the coder CLI client (pure decision + stop)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli.client import stop_daemon, workspace_mismatch, workspace_port
from autobot.daemon import registry
from autobot.daemon.pidfile import write_pidfile


def test_workspace_mismatch_canonicalizes(tmp_path: Path) -> None:
    a = tmp_path / "proj"
    a.mkdir()
    assert workspace_mismatch(str(a), str(a) + "/") is False  # same dir, trailing slash
    assert workspace_mismatch(str(a), str(tmp_path / "other")) is True


def test_stop_daemon_signals_recorded_pid(tmp_path: Path) -> None:
    p = tmp_path / "coder-daemon.pid"
    write_pidfile(9999, "/ws", 8766, path=p)
    killed: list[int] = []
    ok = stop_daemon(pidfile_path=p, kill=lambda pid, sig: killed.append(pid))
    assert ok is True and killed == [9999]
    assert not p.exists()  # file removed after stop


def test_stop_daemon_no_pidfile_is_false(tmp_path: Path) -> None:
    ok = stop_daemon(pidfile_path=tmp_path / "missing.pid", kill=lambda pid, sig: None)
    assert ok is False


def test_workspace_port_uses_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = str((tmp_path / "proj").resolve())
    monkeypatch.setattr(registry, "entry", lambda w, **k: {"port": 8888, "pid": 1})
    assert workspace_port(ws) == 8888  # a recorded entry wins
    monkeypatch.setattr(registry, "entry", lambda w, **k: None)
    monkeypatch.setattr(registry, "read", lambda **k: {})
    assert 8770 <= workspace_port(ws) <= 8899  # else a hashed free port in range


def test_stop_daemon_falls_back_to_port_when_no_pidfile(tmp_path: Path) -> None:
    # A daemon from before this version wrote no pid file; stop it via the port listener.
    killed: list[int] = []
    ok = stop_daemon(
        port=8766,
        pidfile_path=tmp_path / "missing.pid",
        kill=lambda pid, sig: killed.append(pid),
        pid_on_port=lambda port: 4242,
    )
    assert ok is True and killed == [4242]
