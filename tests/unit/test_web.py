"""Tests for the opt-in web-search tool (injected backend, no network)."""

from __future__ import annotations

from autobot.config import Settings
from autobot.tools.registry import ToolRegistry
from autobot.tools.web import WebSearchTool


def _fake_results(_query: str, _max: int) -> list[dict[str, str]]:
    return [
        {"title": "Bengaluru weather", "body": "28°C, clear.", "href": "https://ex/1"},
        {"title": "Forecast", "body": "Rain tomorrow.", "href": "https://ex/2"},
    ]


def _tool(**overrides: object) -> WebSearchTool:
    return WebSearchTool(Settings(**overrides), search_fn=_fake_results)  # type: ignore[arg-type]


def test_search_returns_url_free_context_for_the_llm() -> None:
    out = _tool(web_results=2).search("weather in bengaluru")
    assert "Bengaluru weather" in out
    assert "28°C, clear." in out
    # No URLs and no numbered list — the model should speak a natural summary.
    assert "https://" not in out
    assert "1." not in out


def test_empty_query_is_handled() -> None:
    assert "No query" in _tool().search("   ")


def test_no_results_message() -> None:
    tool = WebSearchTool(Settings(), search_fn=lambda _q, _m: [])
    assert "No web results" in tool.search("asdfqwerty")


def test_registers_web_search_tool() -> None:
    registry = ToolRegistry()
    for spec in _tool().specs():
        registry.register(spec)
    assert registry.get("web_search") is not None
