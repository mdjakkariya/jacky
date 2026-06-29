"""On-device tool selection: pick a bounded, relevant subset to advertise.

Advertising every registered tool on every turn bloats context linearly with the
tool count — costly, and (on a small local model) accuracy-degrading once the set
passes a few dozen tools. This module ranks the **gated** tools by a lightweight,
dependency-free keyword relevance (term overlap with IDF weighting and a name-match
boost) and returns the always-on **core** tools plus the top matches, bounded by a
budget. Everything here is pure/synchronous so it is unit-tested without a model.

The ``mcp`` SDK is irrelevant here; this operates only on registered ``ToolSpec``s.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.config import Settings
    from autobot.core.interfaces import ToolSelector
    from autobot.tools.registry import ToolRegistry, ToolSpec

# Words too common to carry intent; dropped before scoring so they don't inflate
# overlap. Deliberately small — the IDF weighting already down-weights frequent terms.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "my",
        "is",
        "it",
        "and",
        "or",
        "do",
        "you",
        "can",
        "could",
        "would",
        "will",
        "please",
        "i",
        "me",
        "for",
        "on",
        "in",
        "with",
        "this",
        "that",
        "your",
        "whats",
    }
)
_NAME_BOOST = 2.0  # a query term matching the tool *name* counts double


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop short tokens and stopwords."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def score_tools(query: str, specs: Sequence[ToolSpec]) -> list[tuple[ToolSpec, float]]:
    """Rank ``specs`` by keyword relevance to ``query`` (desc, then name asc).

    Each tool's document is its name (split on ``_``) plus its description. Score is
    the IDF-weighted overlap of query terms with the document, with name matches
    boosted. Zero-score specs are excluded — better to advertise fewer tools than to
    pad the context with irrelevant ones. ``query`` with no usable terms → ``[]``.
    """
    q = set(tokenize(query))
    if not q or not specs:
        return []
    docs: list[tuple[ToolSpec, set[str], set[str]]] = []
    df: dict[str, int] = {}
    for s in specs:
        name_tokens = set(tokenize(s.name.replace("_", " ")))
        doc = name_tokens | set(tokenize(s.description))
        docs.append((s, name_tokens, doc))
        for t in doc:
            df[t] = df.get(t, 0) + 1
    n = len(specs)
    scored: list[tuple[ToolSpec, float]] = []
    for s, name_tokens, doc in docs:
        score = 0.0
        for t in q & doc:
            idf = math.log(1 + n / (1 + df[t]))
            score += idf * (_NAME_BOOST if t in name_tokens else 1.0)
        if score > 0:
            scored.append((s, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].name))
    return scored


class AllToolsSelector:
    """Advertises every registered tool (the pre-optimization behavior).

    Used when ``tool_selection == "all"`` — a debugging/comparison escape hatch.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return every registered spec, ignoring ``query``/``pinned``."""
        return self._registry.specs()

    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Top ``limit`` tool names by relevance to ``intent`` (all tools ranked).

        The "all" mode draws no core/gated line, so every registered tool is a
        candidate. Used only as the ``find_tools`` backend when gating is disabled.
        """
        ranked = score_tools(intent, self._registry.specs())
        return [spec.name for spec, _ in ranked[:limit]]


class LexicalToolSelector:
    """Relevance-gated tool advertising via on-device keyword ranking.

    Always advertises the core set; fills the remaining budget with the gated tools
    most relevant to the user's message (per :func:`score_tools`); force-includes any
    pinned tools. ``core_extra``/``core_remove`` adjust the core set from settings
    without code edits. Reads the live registry each call, so MCP tools that connect
    or disconnect are picked up automatically.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        budget: int,
        core_extra: frozenset[str],
        core_remove: frozenset[str],
    ) -> None:
        self._registry = registry
        self._budget = budget
        self._core_extra = core_extra
        self._core_remove = core_remove

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return core U top-K relevant gated U pinned, deduped and budget-bounded."""
        specs = self._registry.specs()
        core_names = ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove
        core = [s for s in specs if s.name in core_names]
        gated = [s for s in specs if s.name not in core_names]

        k = max(0, self._budget - len(core))
        ranked = [s for s, _ in score_tools(query, gated)][:k]

        pinned_specs = [s for s in specs if s.name in pinned and s.name not in core_names]

        chosen: list[ToolSpec] = []
        seen: set[str] = set()
        for s in (*core, *ranked, *pinned_specs):
            if s.name not in seen:
                seen.add(s.name)
                chosen.append(s)
        return chosen

    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Top ``limit`` *gated* tool names by relevance to ``intent``.

        Core tools are excluded — the model already sees them every round, so a
        discovery query should only surface the gated tools it can't currently
        reach. ``core_extra``/``core_remove`` shift the core boundary the same way
        they do in :meth:`select`.
        """
        specs = self._registry.specs()
        core_names = ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove
        gated = [s for s in specs if s.name not in core_names]
        ranked = score_tools(intent, gated)
        return [spec.name for spec, _ in ranked[:limit]]


def build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector:
    """Construct the configured selector. ``"all"`` → advertise everything.

    Any value other than ``"all"`` (including the default ``"lexical"`` and the
    Phase-4 ``"embedding"`` placeholder) currently builds the lexical selector.
    """
    if settings.tool_selection == "all":
        return AllToolsSelector(registry)
    return LexicalToolSelector(
        registry,
        budget=settings.tool_budget,
        core_extra=frozenset(settings.tool_core_extra),
        core_remove=frozenset(settings.tool_core_remove),
    )
