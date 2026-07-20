"""Tests for the opt-in web-search tool: parsing, formatting, and fallback."""

from __future__ import annotations

from autobot.config import Settings
from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry
from autobot.tools.web import (
    WebSearchTool,
    html_to_text,
    parse_searchspace,
    register_web_fetch,
    web_fetch,
)


def _fake_results(_query: str, _max: int) -> list[dict[str, str]]:
    return [
        {"title": "Bengaluru weather", "body": "28°C, clear.", "href": "https://ex/1"},
        {"title": "Forecast", "body": "Rain tomorrow.", "href": "https://ex/2"},
    ]


def _tool(primary: object = None, fallback: object = None, **overrides: object) -> WebSearchTool:
    # Default to the keyless "ddgs" provider so the default-primary path never
    # reads the real Keychain / hits the network during tests (isolation).
    overrides.setdefault("web_provider", "ddgs")
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


# --- web_fetch (G10) -----------------------------------------------------------------------


def test_html_to_text_strips_tags_and_scripts() -> None:
    html = (
        "<html><head><style>x{}</style></head><body><h1>Title</h1>"
        "<p>Hello <b>world</b></p><script>evil()</script></body></html>"
    )
    text = html_to_text(html)
    assert "Title" in text and "Hello" in text and "world" in text
    assert "evil" not in text and "x{}" not in text  # script/style dropped


def test_web_fetch_converts_html() -> None:
    def fake_get(url: str, timeout: float) -> tuple[str, bytes]:
        return "text/html", b"<p>Docs: <a href='x'>read</a> me</p>"

    out = web_fetch("https://example.com/docs", getter=fake_get)
    assert "example.com/docs" in out
    assert "Docs" in out and "read" in out and "<p>" not in out  # tags stripped


def test_web_fetch_plain_text_passthrough() -> None:
    def fake_get(url: str, timeout: float) -> tuple[str, bytes]:
        return "text/plain", b"raw log line\nsecond line"

    out = web_fetch("https://example.com/log", getter=fake_get)
    assert "raw log line" in out and "second line" in out


def test_web_fetch_rejects_non_http_scheme() -> None:
    out = web_fetch("file:///etc/passwd", getter=lambda u, t: ("text/plain", b"secret"))
    assert "http" in out.lower()
    assert "secret" not in out  # never fetched


def test_web_fetch_reports_network_error() -> None:
    def boom(url: str, timeout: float) -> tuple[str, bytes]:
        raise OSError("connection refused")

    assert "couldn't fetch" in web_fetch("https://example.com", getter=boom).lower()


def test_web_fetch_truncates_long_text() -> None:
    def fake_get(url: str, timeout: float) -> tuple[str, bytes]:
        return "text/plain", b"z" * 100_000

    out = web_fetch("https://example.com", getter=fake_get, max_chars=1000)
    assert "truncated" in out.lower()


def test_register_web_fetch_is_network_read_only() -> None:
    reg = ToolRegistry()
    register_web_fetch(reg, Settings(allow_web=True))
    spec = reg.get("web_fetch")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY
    assert spec.network is True  # off-device egress disclosed + audited
