from __future__ import annotations

from pathlib import Path

from autobot.agent.session_store import SessionStore


def test_create_then_append_then_load_roundtrips(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/proj", model="gpt-x")
    assert s.id and s.cwd == "/proj" and s.model == "gpt-x"
    store.append(s, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}])
    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.id == s.id and loaded.cwd == "/proj" and loaded.model == "gpt-x"
    assert loaded.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
    ]


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert SessionStore(str(tmp_path)).load("nope") is None


def test_list_reports_sessions_most_recent_first(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    a = store.create(cwd="/a", model="m")
    store.append(a, [{"role": "user", "content": "1"}])
    b = store.create(cwd="/b", model="m")
    store.append(b, [{"role": "user", "content": "2"}])
    listed = store.list()
    ids = [row["id"] for row in listed]
    assert set(ids) == {a.id, b.id}
    assert all({"id", "cwd", "model"} <= row.keys() for row in listed)


def test_append_is_incremental_not_rewrite(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/p", model="m")
    store.append(s, [{"role": "user", "content": "one"}])
    store.append(s, [{"role": "assistant", "content": "two"}])
    loaded = store.load(s.id)
    assert loaded is not None
    assert [m["content"] for m in loaded.history] == ["one", "two"]
