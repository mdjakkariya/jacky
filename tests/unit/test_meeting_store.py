"""Tests for MeetingStore."""

from __future__ import annotations

from datetime import datetime

from autobot.meeting.store import MeetingStore


def _store(tmp_path: object, t: datetime = datetime(2026, 6, 30, 10, 15)) -> MeetingStore:
    return MeetingStore(str(tmp_path), now=lambda: t)


def test_create_makes_slugged_folder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _store(tmp_path).create("Daily Standup!")
    assert paths.id == "2026-06-30-1015-daily-standup"
    assert paths.dir.endswith("2026-06-30-1015-daily-standup")
    assert paths.near_wav.endswith("near.wav") and paths.far_wav.endswith("far.wav")


def test_manifest_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    paths = store.create("x")
    store.write_manifest(paths, {"id": paths.id, "state": "done", "title": "x"})
    assert store.read_manifest(paths.dir)["state"] == "done"


def test_find_interrupted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    p = store.create("m")
    store.write_manifest(p, {"id": p.id, "state": "recording"})
    assert store.find_interrupted() == [p.id]
    store.write_manifest(p, {"id": p.id, "state": "done"})
    assert store.find_interrupted() == []


def test_prune_keeps_most_recent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = MeetingStore(str(tmp_path))
    ids = []
    for i in range(5):
        p = store.create(f"m{i}")
        store.write_manifest(p, {"id": p.id, "state": "done"})
        ids.append(p.id)
    removed = store.prune(keep=2)
    assert len(removed) == 3
    assert len(store.list_recent()) == 2
