"""`jack cost` reads the global ledger directly (no daemon) and renders/opens it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import autobot.usage.report as report
from autobot.cli import _run_cost
from autobot.usage import ledger


def _seed(path: Path) -> None:
    ledger.append(
        ledger.UsageEntry(
            ts="2026-07-14T12:00:00Z",
            provider="anthropic",
            model="claude-sonnet-5",
            workspace="/w",
            session_id="s1",
            in_tokens=10,
            out_tokens=20,
            cache_read=5,
            cache_write=3,
            usd=0.5,
            priced=True,
        ),
        path=path,
    )


def test_jack_cost_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "usage.jsonl"
    _seed(p)
    monkeypatch.setattr(ledger, "default_path", lambda: p)
    rc = _run_cost([])
    assert rc == 0
    assert "claude-sonnet-5" in capsys.readouterr().out


def test_jack_cost_open_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "usage.jsonl"
    _seed(p)
    monkeypatch.setattr(ledger, "default_path", lambda: p)
    captured: dict[str, Any] = {}

    def _fake_write_and_open(
        rollups: dict[str, Any], *, now: Any, dest: Any = None, open_browser: Any = None
    ) -> Path:
        captured["rollups"] = rollups
        return tmp_path / "r.html"

    monkeypatch.setattr(report, "write_and_open", _fake_write_and_open)
    rc = _run_cost(["--open"])
    assert rc == 0 and "rollups" in captured
