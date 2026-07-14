"""The /cost command renders a usage summary; /cost open builds+opens the dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console

from autobot.cli import coder_commands, render


def _payload() -> dict[str, Any]:
    b: dict[str, Any] = {
        "turns": 5,
        "in": 100,
        "out": 200,
        "cache_read": 1000,
        "cache_write": 500,
        "tokens": 300,
        "usd": 0.42,
        "has_unpriced": False,
    }
    return {
        "ctx": {"model": "claude-sonnet-5", "used": 1400, "window": 1_000_000},
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "rollups": {
            "totals": {"today": b, "last_7d": b, "last_30d": b, "all_time": b},
            "daily": [],
            "by_model": [{"key": "claude-sonnet-5", **b}],
            "by_provider": [{"key": "anthropic", **b}],
            "by_workspace": [{"key": "/w", **b}],
            "session": b,
        },
    }


def _text(renderable: Any) -> str:
    con = Console(width=90)
    with con.capture() as cap:
        con.print(renderable)
    return cap.get()


def test_render_cost_shows_session_and_totals() -> None:
    out = _text(render.render_cost(_payload(), 90))
    assert "claude-sonnet-5" in out
    assert "$0.42" in out
    assert "Today" in out and "All time" in out


def test_render_cost_handles_no_data() -> None:
    empty: dict[str, Any] = {
        "ctx": None,
        "provider": "anthropic",
        "model": None,
        "rollups": {
            "totals": {},
            "daily": [],
            "by_model": [],
            "by_provider": [],
            "by_workspace": [],
            "session": None,
        },
    }
    out = _text(render.render_cost(empty, 90))
    assert "No usage" in out


def test_cost_command_dispatches() -> None:
    got = coder_commands.handle(
        "/cost",
        "",
        base_url="http://x",
        cwd="/w",
        width=90,
        deps=coder_commands.Deps(get_usage=lambda _b: _payload()),
    )
    assert got is not None  # a renderable, not None (which would fall through)


def test_cost_open_builds_and_opens(tmp_path: Path) -> None:
    opened: list[str] = []

    def _open(rollups: dict[str, Any]) -> str:
        opened.append("opened")
        return "/tmp/r.html"

    deps = coder_commands.Deps(get_usage=lambda _b: _payload(), open_report=_open)
    msg = coder_commands.handle("/cost", "open", base_url="http://x", cwd="/w", width=90, deps=deps)
    assert opened == ["opened"]
    assert isinstance(msg, str) and "report" in msg.lower()
