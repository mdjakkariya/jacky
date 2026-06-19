"""Tests for the opt-in web-search tool: parsing, formatting, and fallback."""

from __future__ import annotations

from autobot.config import Settings
from autobot.tools.registry import ToolRegistry
from autobot.tools.web import WebSearchTool, parse_searchspace


def _fake_results(_query: str, _max: int) -> list[dict[str, str]]:
    return [
        {"title": "Bengaluru weather", "body": "28°C, clear.", "href": "https://ex/1"},
        {"title": "Forecast", "body": "Rain tomorrow.", "href": "https://ex/2"},
    ]


def _tool(primary: object = None, fallback: object = None, **overrides: object) -> WebSearchTool:
    return WebSearchTool(
        Settings(**overrides),  # type: ignore[arg-type]
        primary=primary,  # type: ignore[arg-type]
        fallback=fallback or _fake_results,  # type: ignore[arg-type]
    )


def test_parse_searchspace_maps_fields() -> None:
    payload = {
        "results": [
            {"title": "T", "snippet": "S", "url": "https://u", "score": 0.9},
        ],
        "latency_ms": 38,
    }
    assert parse_searchspace(payload) == [{"title": "T", "body": "S", "href": "https://u"}]


def test_parse_searchspace_empty() -> None:
    assert parse_searchspace({}) == []


def test_search_formats_url_free_context() -> None:
    out = _tool().search("weather in bengaluru")
    assert "Bengaluru weather" in out and "28°C, clear." in out
    assert "https://" not in out and "1." not in out


def test_empty_query_is_handled() -> None:
    assert "No query" in _tool().search("   ")


def test_primary_used_when_it_returns_results() -> None:
    def primary(_q: str, _m: int) -> list[dict[str, str]]:
        return [{"title": "API", "body": "from api", "href": "https://api"}]

    out = _tool(primary=primary).search("x")
    assert "from api" in out


def test_falls_back_to_ddgs_when_primary_raises() -> None:
    def boom(_q: str, _m: int) -> list[dict[str, str]]:
        raise RuntimeError("api down")

    # primary raises -> the fallback (_fake_results) is used.
    out = _tool(primary=boom).search("x")
    assert "Bengaluru weather" in out


def test_falls_back_when_primary_empty() -> None:
    out = _tool(primary=lambda _q, _m: []).search("x")
    assert "Bengaluru weather" in out


def test_registers_web_search_tool() -> None:
    registry = ToolRegistry()
    for spec in _tool().specs():
        registry.register(spec)
    assert registry.get("web_search") is not None
