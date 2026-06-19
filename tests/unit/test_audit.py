"""Tests for the SQLite audit log."""

from __future__ import annotations

from autobot.core.types import Decision
from autobot.tools.audit import AuditLog


def _log() -> AuditLog:
    return AuditLog(":memory:")


def test_record_and_read_back() -> None:
    log = _log()
    log.log(
        tool="delete_file",
        arguments={"path": "x.txt"},
        risk="DESTRUCTIVE",
        decision=Decision.ALLOWED,
        ok=True,
        detail="deleted x.txt",
    )
    entries = log.recent()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.tool == "delete_file"
    assert entry.arguments == {"path": "x.txt"}
    assert entry.decision is Decision.ALLOWED
    assert entry.ok is True


def test_recent_is_newest_first_and_limited() -> None:
    log = _log()
    for i in range(5):
        log.log(
            tool=f"t{i}",
            arguments={},
            risk="WRITE",
            decision=Decision.ALLOWED,
            ok=True,
            detail="",
        )
    entries = log.recent(limit=3)
    assert [e.tool for e in entries] == ["t4", "t3", "t2"]


def test_denied_entry_has_null_ok() -> None:
    log = _log()
    log.log(
        tool="delete_file",
        arguments={"path": "x"},
        risk="DESTRUCTIVE",
        decision=Decision.DENIED,
        ok=None,
        detail="declined by user",
    )
    assert log.recent()[0].ok is None
