"""JackApp: submit → render, plan→approve→done, and a slash command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.widgets import Input

_Post = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _fake_post(script: list[dict[str, Any]]) -> tuple[_Post, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append((url, payload))
        return script[len(calls) - 1]

    return post, calls


async def test_done_turn_renders_reply() -> None:
    from autobot.cli.tui import JackApp

    post, _ = _fake_post([{"status": "done", "reply": "All set."}])
    app = JackApp("http://x", ".", post=post, snapshot=lambda c: None, diff_since=lambda c, b: None)
    async with app.run_test() as pilot:
        app.query_one("#input", Input).value = "do a thing"
        await pilot.press("enter")
        await pilot.pause()
        assert "All set." in app.transcript_text()


async def test_plan_then_approve_reaches_done() -> None:
    from autobot.cli.tui import JackApp

    post, calls = _fake_post(
        [
            {"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]},
            {"status": "done", "reply": "Edited foo."},
        ]
    )
    app = JackApp("http://x", ".", post=post, snapshot=lambda c: None, diff_since=lambda c, b: None)
    async with app.run_test() as pilot:
        app.query_one("#input", Input).value = "edit foo"
        await pilot.press("enter")
        await pilot.pause()
        assert "edit foo" in app.transcript_text()  # plan shown
        app.query_one("#input", Input).value = "y"
        await pilot.press("enter")
        await pilot.pause()
        assert "Edited foo." in app.transcript_text()
        assert calls[1][0].endswith("/coder/reply") and calls[1][1]["value"] == "approve"


async def test_help_command_lists_commands() -> None:
    from autobot.cli.tui import JackApp

    post, _ = _fake_post([])
    app = JackApp("http://x", ".", post=post, snapshot=lambda c: None, diff_since=lambda c, b: None)
    async with app.run_test() as pilot:
        app.query_one("#input", Input).value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        assert "/exit" in app.transcript_text()
