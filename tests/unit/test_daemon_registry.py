"""The per-workspace coder-daemon registry + port allocation."""

from __future__ import annotations

from pathlib import Path

from autobot.daemon import registry


def test_record_entry_remove_roundtrip(tmp_path: Path) -> None:
    reg = tmp_path / "daemons.json"
    ws = tmp_path / "proj"
    ws.mkdir()
    assert registry.entry(ws, path=reg) is None
    registry.record(ws, 8771, 4242, path=reg)
    assert registry.entry(ws, path=reg) == {"port": 8771, "pid": 4242}
    assert registry.entry(str(ws) + "/", path=reg) == {"port": 8771, "pid": 4242}  # resolves same
    registry.remove(ws, path=reg)
    assert registry.entry(ws, path=reg) is None


def test_port_for_is_stable_and_in_range(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    p1 = registry.port_for(ws, set())
    p2 = registry.port_for(ws, set())
    assert p1 == p2  # deterministic
    assert 8770 <= p1 <= 8899


def test_port_for_avoids_taken(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    base = registry.port_for(ws, set())
    assert registry.port_for(ws, {base}) != base  # steps to the next free port


def test_malformed_registry_is_empty(tmp_path: Path) -> None:
    reg = tmp_path / "daemons.json"
    reg.write_text("{not json", encoding="utf-8")
    assert registry.read(path=reg) == {}
