"""Tests for the append-only usage ledger (best-effort file I/O)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from autobot.usage.ledger import UsageEntry, append, read


def _entry(**kw: object) -> UsageEntry:
    base = {
        "ts": "2026-07-14T16:00:00Z",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "workspace": "/w",
        "session_id": "s1",
        "in_tokens": 10,
        "out_tokens": 20,
        "cache_read": 5,
        "cache_write": 3,
        "usd": 0.5,
        "priced": True,
    }
    base.update(kw)
    return UsageEntry(**base)  # type: ignore[arg-type]


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    append(_entry(), path=p)
    append(_entry(session_id="s2", usd=None, priced=False), path=p)
    rows = read(path=p)
    assert [r.session_id for r in rows] == ["s1", "s2"]
    assert rows[0].usd == 0.5 and rows[1].usd is None and rows[1].priced is False


def test_row_uses_short_in_out_keys_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    append(_entry(), path=p)
    line = p.read_text(encoding="utf-8").strip()
    assert '"in": 10' in line and '"out": 20' in line and '"in_tokens"' not in line


def test_read_skips_corrupt_lines(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    append(_entry(), path=p)
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")  # a torn/partial line from a crash
    append(_entry(session_id="s3"), path=p)
    rows = read(path=p)
    assert [r.session_id for r in rows] == ["s1", "s3"]


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert read(path=tmp_path / "nope.jsonl") == []


def test_append_never_raises_on_unwritable_path(tmp_path: Path) -> None:
    # A directory where a file is expected: append must swallow the error, not raise.
    bad = tmp_path / "adir"
    bad.mkdir()
    append(_entry(), path=bad)  # must not raise


def test_since_filters_by_timestamp(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    append(_entry(ts="2026-07-10T09:00:00Z", session_id="old"), path=p)
    append(_entry(ts="2026-07-14T09:00:00Z", session_id="new"), path=p)
    rows = read(path=p, since=datetime(2026, 7, 12, tzinfo=timezone.utc))
    assert [r.session_id for r in rows] == ["new"]
