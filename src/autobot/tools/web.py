"""Web search — the one tool that reaches off the device.

Everything else in Autobot is on-device; this tool sends the search *query* to a
web search provider. It is strictly opt-in (only registered when
``settings.allow_web`` is true) and every call is audited.

Backends are pluggable and selected by ``settings.web_provider``:

* a keyed HTTP API (default SearchSpace; endpoint + key are configurable) gives
  clean, current results, and
* ddgs scraping is the no-key **fallback** — used automatically when no API key
  is set, or when the API call fails/returns nothing.

The API key comes only from the environment (``AUTOBOT_WEB_API_KEY``); it is
never stored in code or config. Result parsing is split into pure functions so
it can be unit-tested without network access.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from autobot.config import Settings
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("web")

# A search function: (query, max_results) -> list of {"title","body","href"} dicts.
SearchFn = Callable[[str, int], list[dict[str, str]]]


def parse_searchspace(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Map a SearchSpace JSON response to our {title, body, href} result shape."""
    return [
        {
            "title": str(r.get("title", "")),
            "body": str(r.get("snippet", "")),
            "href": str(r.get("url", "")),
        }
        for r in payload.get("results", [])
    ]


class SearchSpaceBackend:
    """Keyed HTTP search via SearchSpace (or any compatible endpoint)."""

    def __init__(self, api_url: str, api_key: str, timeout_s: float = 10.0) -> None:
        self._url = api_url
        self._key = api_key.strip()  # tolerate stray whitespace/newline from env
        self._timeout = timeout_s

    def search(self, query: str, max_results: int) -> list[dict[str, str]]:
        """POST the query and return parsed results.

        Raises with the server's error body on HTTP errors, so a 401/403 shows
        *why* (invalid key, quota, blocked) instead of a bare status code.
        """
        body = json.dumps({"query": query, "top_k": max_results}).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "authorization": f"Bearer {self._key}",
                "content-type": "application/json",
                # A real UA avoids edge/WAF blocks that reject default urllib.
                "user-agent": "autobot/0.1 (+https://searchspace.io)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200].strip()
            raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        return parse_searchspace(payload)


class WebSearchTool:
    """Opt-in, audited web search with a keyed primary backend and ddgs fallback."""

    def __init__(
        self,
        settings: Settings,
        primary: SearchFn | None = None,
        fallback: SearchFn | None = None,
    ) -> None:
        self._max_results = settings.web_results
        self._backend = settings.web_backend
        self._primary = primary if primary is not None else self._default_primary(settings)
        self._fallback = fallback if fallback is not None else self._ddgs_search

    @staticmethod
    def _default_primary(settings: Settings) -> SearchFn | None:
        """Use the keyed API when configured; otherwise there's no primary (ddgs only)."""
        from autobot.secrets import get_secret

        key = get_secret("web_api_key")
        if settings.web_provider == "ddgs" or not key:
            return None
        return SearchSpaceBackend(settings.web_api_url, key).search

    def _ddgs_search(self, query: str, max_results: int) -> list[dict[str, str]]:
        """No-key fallback: ddgs, rotating across engines (lazy import)."""
        from ddgs import DDGS

        with DDGS() as ddgs:
            rows = ddgs.text(
                query, max_results=max_results, backend=self._backend, safesearch="off"
            )
        return [
            {"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")}
            for r in rows
        ]

    def search(self, query: str) -> str:
        """Search the web and return formatted top results for the LLM to summarize."""
        query = query.strip()
        if not query:
            return "No query provided."
        _log.info("web search query=%r max=%d (leaves device)", query, self._max_results)
        results = self._run(query)
        if not results:
            return f"No web results for {query!r}."
        snippets = [
            text for r in results if (text := f"{r.get('title', '')}. {r.get('body', '')}".strip())
        ]
        return (
            f"Here is what web search found for '{query}'. Summarize it for the user "
            "in a natural spoken sentence or two, without mentioning sources or URLs:\n"
            + "\n".join(snippets)
        )

    def _run(self, query: str) -> list[dict[str, str]]:
        """Try the primary backend; fall back to ddgs on error or empty results."""
        if self._primary is not None:
            try:
                results = self._primary(query, self._max_results)
                if results:
                    _log.info("web via=api results=%d", len(results))
                    print(f"[web] answered via API ({len(results)} results).")
                    return results
                _log.warning("api returned no results; falling back to ddgs")
                print("[web] API returned no results — using ddgs fallback.")
            except Exception as exc:
                _log.exception("api search failed; falling back to ddgs")
                print(f"[web] API search failed ({exc}) — using ddgs fallback.")
        try:
            results = self._fallback(query, self._max_results)
            _log.info("web via=ddgs results=%d", len(results))
            return results
        except Exception:
            _log.exception("web search (fallback) failed query=%r", query)
            return []

    def specs(self) -> list[ToolSpec]:
        """Return the tool spec for web search (read-only locally; network egress)."""
        return [
            ToolSpec(
                name="web_search",
                description=(
                    "Search the web for current, recent, or time-sensitive "
                    "information — news, sports scores, weather, prices, today's "
                    "events, or any fact you're not certain of. Prefer calling this "
                    "over answering from memory or saying you don't know."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "The search query."}},
                    "required": ["query"],
                },
                handler=self.search,
                risk=Risk.READ_ONLY,
                core=True,
            )
        ]


def register_web_tools(registry: ToolRegistry, settings: Settings) -> WebSearchTool:
    """Register the web-search tool. Call only when ``settings.allow_web`` is true."""
    tool = WebSearchTool(settings)
    for spec in tool.specs():
        registry.register(spec)
    return tool


# --- web_fetch: read a URL's content (the coder's off-device read, opt-in) -----------------

_FETCH_TIMEOUT_S = 10.0
_MAX_FETCH_BYTES = 2_000_000  # cap the download so a huge page can't blow memory/context
_FETCH_MAX_CHARS = 50_000  # cap the text handed to the model

# (url, timeout_s) -> (content_type, body_bytes). Injectable so tests never hit the network.
Fetcher = Callable[[str, float], "tuple[str, bytes]"]


class _HtmlTextExtractor(HTMLParser):
    """Collect visible text from HTML, dropping script/style/head content."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter a skipped region (script/style/…) or emit a break for block tags."""
        if tag in ("script", "style", "noscript", "head", "template"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave a skipped region."""
        if tag in ("script", "style", "noscript", "head", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        """Collect text outside skipped regions."""
        if self._skip == 0 and data.strip():
            self._parts.append(data.strip() + " ")

    def text(self) -> str:
        """The collapsed plain text extracted so far."""
        lines = [ln.strip() for ln in "".join(self._parts).splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()


def html_to_text(html: str) -> str:
    """Extract readable plain text from an HTML document (pure; never raises)."""
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
    except Exception:  # a malformed document must not raise out of a tool
        _log.debug("html parse fell back to raw", exc_info=True)
    return parser.text()


def _http_get(url: str, timeout_s: float) -> tuple[str, bytes]:  # pragma: no cover - real network
    """Fetch ``url`` and return its content type and (bounded) body bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "jack-web-fetch/1"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.headers.get_content_type(), resp.read(_MAX_FETCH_BYTES + 1)


def web_fetch(
    url: str,
    *,
    getter: Fetcher = _http_get,
    timeout_s: float = _FETCH_TIMEOUT_S,
    max_chars: int = _FETCH_MAX_CHARS,
) -> str:
    """Fetch a URL and return its text (HTML converted to readable text), bounded.

    Off-device: sends the URL to the network. Only http/https are allowed. ``getter`` is
    injectable so the network boundary is unit-tested without a real request.
    """
    if not url or not url.strip():
        return "What URL should I fetch?"
    if urlparse(url).scheme not in ("http", "https"):
        return "I can only fetch http/https URLs."
    try:
        content_type, body = getter(url, timeout_s)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"I couldn't fetch that URL: {exc}"
    text = body[:_MAX_FETCH_BYTES].decode("utf-8", errors="replace")
    if "html" in (content_type or ""):
        text = html_to_text(text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(truncated)"
    _log.info("web_fetch url=%s bytes=%d type=%s", url[:120], len(body), content_type)
    if not text:
        return f"Fetched {url} ({content_type}) — no readable text."
    return f"Fetched {url} ({content_type}):\n\n{text}"


def register_web_fetch(registry: ToolRegistry, settings: Settings) -> None:
    """Register the ``web_fetch`` tool. Call only when ``settings.allow_web`` is true.

    Marked ``network=True`` so the UI shows the off-device egress badge and the audit log
    records it — the disclosed, opt-in exception to on-device-only, like ``web_search``.
    """
    registry.register(
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a web page or file by its URL and return the text (HTML is converted to "
                "readable text). Use to read documentation or a page you have a link to. Sends "
                "the URL off the device."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL to fetch."}},
                "required": ["url"],
            },
            handler=lambda url="": web_fetch(url),
            risk=Risk.READ_ONLY,
            network=True,
        )
    )
