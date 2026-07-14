"""Tests for the self-contained, on-device HTML report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autobot.usage.report import build_html, write_and_open

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _rollups(usd: float = 1.37) -> dict[str, Any]:
    bucket = {
        "turns": 137,
        "in": 81274,
        "out": 25047,
        "cache_read": 927855,
        "cache_write": 306441,
        "tokens": 106321,
        "usd": usd,
        "has_unpriced": False,
    }
    return {
        "totals": {"today": bucket, "last_7d": bucket, "last_30d": bucket, "all_time": bucket},
        "daily": [
            {
                "date": "2026-07-14",
                "usd": usd,
                "tokens": 106321,
                "turns": 137,
                "has_unpriced": False,
            }
        ],
        "by_model": [
            {
                "key": "claude-sonnet-5",
                "usd": usd,
                "turns": 137,
                "tokens": 106321,
                "has_unpriced": False,
            }
        ],
        "by_provider": [
            {"key": "anthropic", "usd": usd, "turns": 137, "tokens": 106321, "has_unpriced": False}
        ],
        "by_workspace": [
            {
                "key": "/w/a11y demo",
                "usd": usd,
                "turns": 137,
                "tokens": 106321,
                "has_unpriced": False,
            }
        ],
        "session": None,
    }


def test_html_contains_totals_and_svg() -> None:
    html = build_html(_rollups(), now=NOW)
    assert "claude-sonnet-5" in html
    assert "$1.37" in html
    assert "<svg" in html  # the daily chart is inline SVG


def test_html_is_on_device_only() -> None:
    # The privacy invariant: no external requests of any kind.
    html = build_html(_rollups(), now=NOW).lower()
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html
    assert "cdn" not in html


def test_empty_ledger_renders_a_friendly_page() -> None:
    empty: dict[str, Any] = {
        "totals": {
            k: {
                "turns": 0,
                "in": 0,
                "out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "tokens": 0,
                "usd": 0.0,
                "has_unpriced": False,
            }
            for k in ("today", "last_7d", "last_30d", "all_time")
        },
        "daily": [],
        "by_model": [],
        "by_provider": [],
        "by_workspace": [],
        "session": None,
    }
    html = build_html(empty, now=NOW)
    assert "No usage recorded yet" in html


def test_write_and_open_writes_file_and_calls_opener(tmp_path: Path) -> None:
    opened: list[str] = []

    def _open(url: str) -> bool:
        opened.append(url)
        return True

    dest = tmp_path / "report.html"
    out = write_and_open(_rollups(), now=NOW, dest=dest, open_browser=_open)
    assert out == dest and dest.exists()
    assert opened and opened[0].startswith("file://")
