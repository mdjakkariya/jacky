from __future__ import annotations

from pathlib import Path

from autobot.daemon.pidfile import read_pidfile, remove_pidfile, write_pidfile


def test_write_read_remove_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "coder-daemon.pid"
    write_pidfile(4321, "/ws/proj", 8766, path=p)
    got = read_pidfile(path=p)
    assert got == {"pid": 4321, "workspace": "/ws/proj", "port": 8766}
    remove_pidfile(path=p)
    assert read_pidfile(path=p) is None


def test_read_missing_or_malformed_is_none(tmp_path: Path) -> None:
    p = tmp_path / "coder-daemon.pid"
    assert read_pidfile(path=p) is None  # missing
    p.write_text("{not json", encoding="utf-8")
    assert read_pidfile(path=p) is None  # malformed
