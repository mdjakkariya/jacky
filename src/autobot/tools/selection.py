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
