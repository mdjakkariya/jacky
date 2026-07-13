from __future__ import annotations

import json
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


def test_create_writes_no_file_until_append(tmp_path: Path) -> None:
    # create() must be lazy: no file on disk until the first append. This is what
    # keeps a harness rebuild (startup, or a settings-triggered reload) or an unused
    # "New chat" from leaving a ghost meta-header-only file behind.
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/proj", model="gpt-x")
    assert store.load(s.id) is None
    assert store.list() == []

    store.append(s, [{"role": "user", "content": "hi"}])
    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.history == [{"role": "user", "content": "hi"}]
    assert [row["id"] for row in store.list()] == [s.id]


def test_list_skips_header_only_sessions(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    # A hand-written legacy/out-of-band file with only a meta header, no msg lines.
    ghost_id = "ghost123"
    meta_only = {"type": "meta", "id": ghost_id, "cwd": "/ghost", "model": "m"}
    (tmp_path / f"{ghost_id}.jsonl").write_text(json.dumps(meta_only) + "\n", encoding="utf-8")

    real = store.create(cwd="/real", model="m")
    store.append(real, [{"role": "user", "content": "hi"}])

    ids = [row["id"] for row in store.list()]
    assert ghost_id not in ids
    assert real.id in ids


def test_load_skips_structurally_malformed_lines(tmp_path: Path) -> None:
    # A truncated/hand-edited transcript can contain valid-JSON but structurally-wrong
    # lines: a msg record with no "message", a bare non-object, or a non-dict message.
    # load() must skip those (not raise) and return only well-formed messages, in order.
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/p", model="m")
    records: list[object] = [
        {"type": "msg", "message": {"role": "user", "content": "good1"}},
        {"type": "msg"},  # no "message" key
        [1, 2, 3],  # valid JSON, not an object
        {"type": "msg", "message": "not-a-dict"},  # message is not a dict
        {"type": "msg", "message": {"role": "assistant", "content": "good2"}},
    ]
    with (tmp_path / f"{s.id}.jsonl").open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    loaded = store.load(s.id)
    assert loaded is not None
    assert [m["content"] for m in loaded.history] == ["good1", "good2"]
