"""Tests for the one-call recording helper (prices + appends; best-effort)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from autobot.usage.ledger import read
from autobot.usage.record import record_turn

AT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def test_records_a_priced_anthropic_row(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    record_turn(
        provider="anthropic",
        model="claude-sonnet-5",
        workspace="/w",
        session_id="s1",
        in_tokens=1_000_000,
        out_tokens=1_000_000,
        at=AT,
        path=p,
    )
    rows = read(path=p)
    assert len(rows) == 1
    assert rows[0].priced is True and rows[0].usd == 18.0  # list rate $3/$15 (no promo assumed)


def test_records_local_turn_as_zero(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    record_turn(
        provider="ollama",
        model="qwen3:8b",
        workspace="/w",
        session_id="s1",
        in_tokens=500,
        out_tokens=200,
        at=AT,
        path=p,
    )
    rows = read(path=p)
    assert rows[0].usd == 0.0 and rows[0].priced is True


def test_unknown_model_recorded_unpriced(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    record_turn(
        provider="openai",
        model="gpt-unknown",
        workspace="/w",
        session_id="s1",
        in_tokens=10,
        out_tokens=10,
        at=AT,
        path=p,
    )
    rows = read(path=p)
    assert rows[0].usd is None and rows[0].priced is False


def test_disabled_is_a_no_op(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    record_turn(
        provider="anthropic",
        model="claude-sonnet-5",
        workspace="/w",
        session_id="s1",
        in_tokens=10,
        out_tokens=10,
        at=AT,
        enabled=False,
        path=p,
    )
    assert read(path=p) == []


def test_never_raises_on_bad_path(tmp_path: Path) -> None:
    bad = tmp_path / "adir"
    bad.mkdir()
    record_turn(
        provider="anthropic",
        model="claude-sonnet-5",
        workspace="/w",
        session_id="s1",
        in_tokens=10,
        out_tokens=10,
        at=AT,
        path=bad,
    )  # no raise
