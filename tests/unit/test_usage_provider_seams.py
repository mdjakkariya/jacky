"""The provider finalize seams record one row with provider/model/workspace tags."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from autobot.usage.ledger import read
from autobot.usage.record import record_turn

AT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def test_ollama_seam_records_local_zero(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    # Mirrors ollama_llm._report_usage: provider "ollama", model llm_model, cache 0.
    record_turn(
        provider="ollama",
        model="qwen3:8b",
        workspace="/proj",
        session_id="s1",
        in_tokens=1200,
        out_tokens=340,
        at=AT,
        enabled=True,
        path=p,
    )
    rows = read(path=p)
    assert rows[0].provider == "ollama" and rows[0].usd == 0.0 and rows[0].out_tokens == 340


def test_openai_seam_records_unpriced_by_default(tmp_path: Path) -> None:
    p = tmp_path / "usage.jsonl"
    record_turn(
        provider="openai",
        model="gpt-4o-mini",
        workspace="/proj",
        session_id="s1",
        in_tokens=800,
        out_tokens=120,
        at=AT,
        enabled=True,
        path=p,
    )
    rows = read(path=p)
    assert rows[0].provider == "openai" and rows[0].priced is False
