"""Tests for the per-session notification inbox."""

from __future__ import annotations

from autobot.tasks.inbox import NotificationInbox


def test_push_then_drain_returns_notes_in_order() -> None:
    inbox = NotificationInbox()
    inbox.push("s1", "first")
    inbox.push("s1", "second")
    assert inbox.drain("s1") == ["first", "second"]


def test_drain_is_one_shot() -> None:
    inbox = NotificationInbox()
    inbox.push("s1", "note")
    assert inbox.drain("s1") == ["note"]
    assert inbox.drain("s1") == []  # already delivered


def test_notes_are_scoped_per_session() -> None:
    inbox = NotificationInbox()
    inbox.push("s1", "for-s1")
    inbox.push("s2", "for-s2")
    assert inbox.drain("s1") == ["for-s1"]
    assert inbox.drain("s2") == ["for-s2"]


def test_drain_unknown_session_is_empty() -> None:
    inbox = NotificationInbox()
    assert inbox.drain("nope") == []


def test_pending_counts_without_draining() -> None:
    inbox = NotificationInbox()
    assert inbox.pending("s1") == 0
    inbox.push("s1", "a")
    inbox.push("s1", "b")
    assert inbox.pending("s1") == 2
    assert inbox.drain("s1") == ["a", "b"]
    assert inbox.pending("s1") == 0
