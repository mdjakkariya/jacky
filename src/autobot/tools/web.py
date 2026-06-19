"""Web search — the one tool that reaches off the device.

Everything else in Autobot is on-device; this tool sends the search *query* to a
web search engine (DuckDuckGo via the ``ddgs`` package). It is therefore strictly
opt-in: it is only registered when ``settings.allow_web`` is true, and every call
is recorded in the audit log. The tool returns plain-text result snippets; the
local LLM reads them and composes the spoken answer, so no page content is
executed and nothing else leaves the machine.

The search backend is injectable so the formatting logic is unit-tested without
network access.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.config import Settings
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("web")

# A search function: (query, max_results) -> list of {"title","body","href"} dicts.
SearchFn = Callable[[str, int], list[dict[str, str]]]


class WebSearchTool:
    """A sandbox-free, network-reaching search tool (opt-in, audited)."""

    def __init__(self, settings: Settings, search_fn: SearchFn | None = None) -> None:
        self._max_results = settings.web_results
        self._backend = settings.web_backend
        self._search_fn = search_fn or self._ddgs_search

    def _ddgs_search(self, query: str, max_results: int) -> list[dict[str, str]]:
        """Default backend: ddgs, rotating across engines (lazy import)."""
        from ddgs import DDGS

        with DDGS() as ddgs:
            rows = ddgs.text(
                query, max_results=max_results, backend=self._backend, safesearch="off"
            )
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "href": r.get("href", ""),
            }
            for r in rows
        ]

    def search(self, query: str) -> str:
        """Search the web and return formatted top results for the LLM to summarize."""
        query = query.strip()
        if not query:
            return "No query provided."
        _log.info("web search query=%r max=%d (leaves device)", query, self._max_results)
        try:
            results = self._search_fn(query, self._max_results)
        except Exception:
            # Search engines get rate-limited/blocked; degrade gracefully so the
            # model can tell the user instead of relaying a raw error.
            _log.exception("web search failed query=%r backend=%s", query, self._backend)
            return "Web search is unavailable right now (the search engine refused the request)."
        if not results:
            return f"No web results for {query!r}."
        # Plain context for the model to summarize — no numbering and no URLs, so
        # it speaks a natural answer instead of reciting a list of links.
        snippets = [
            body for r in results if (body := f"{r.get('title', '')}. {r.get('body', '')}".strip())
        ]
        return (
            f"Here is what web search found for '{query}'. "
            "Summarize it for the user in a natural spoken sentence or two, "
            "without mentioning sources or URLs:\n" + "\n".join(snippets)
        )

    def specs(self) -> list[ToolSpec]:
        """Return the tool spec for web search (read-only locally; network egress)."""
        return [
            ToolSpec(
                name="web_search",
                description=(
                    "Search the web for current information (news, weather, facts). "
                    "Use when the answer isn't something you already know."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "The search query."}},
                    "required": ["query"],
                },
                handler=self.search,
                risk=Risk.READ_ONLY,
            )
        ]


def register_web_tools(registry: ToolRegistry, settings: Settings) -> WebSearchTool:
    """Register the web-search tool. Call only when ``settings.allow_web`` is true."""
    tool = WebSearchTool(settings)
    for spec in tool.specs():
        registry.register(spec)
    return tool
