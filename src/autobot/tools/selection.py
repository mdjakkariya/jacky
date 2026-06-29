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

import hashlib
import math
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.config import Settings
    from autobot.core.interfaces import ToolSelector
    from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# Type of an injected embedder: maps one text to its embedding vector. The real one
# calls Ollama locally; tests pass a deterministic fake so ranking is unit-testable
# without a live model.
Embedder = Callable[[str], list[float]]

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


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors; ``0.0`` on a zero vector or length mismatch.

    Never raises — a mismatched or zero vector (e.g. an embedder returned ``[]``)
    yields ``0.0`` so a single bad embedding ranks a tool last instead of crashing
    the turn.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embed_doc(spec: ToolSpec) -> str:
    """Return the text embedded to represent ``spec``: its name plus description."""
    return f"{spec.name} {spec.description}".strip()


def _doc_key(spec: ToolSpec) -> str:
    """Stable cache key for ``spec``'s embedding vector.

    Keys on a SHA-256 of :func:`embed_doc` — the exact text we embed. Identical for an
    unchanged tool (so its vector is reused, never re-embedded) and different the moment
    the name or description changes (so it is re-embedded). We hash the doc text rather
    than call :func:`autobot.mcp.adapter.fingerprint` because the registry holds
    :class:`ToolSpec`s, not MCP ``Tool`` objects (no ``inputSchema``/``annotations`` to
    fingerprint), and the doc text is the only embedding-relevant identity here.
    """
    return hashlib.sha256(embed_doc(spec).encode("utf-8")).hexdigest()


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


class EmbeddingToolSelector:
    """Relevance-gated advertising that ranks gated tools by **local** embeddings.

    A recall upgrade over :class:`LexicalToolSelector` for the *local* path only
    (the cloud path uses Anthropic's native tool search and never this). The core
    set, budget, pinning, and ``core_extra``/``core_remove`` rules are identical to
    the lexical selector — only the gated ranking differs: gated tools are ranked by
    cosine similarity of a locally-embedded query against locally-embedded tool docs
    (``name + description``).

    Embedding is done by an injected :data:`Embedder` (so ranking is unit-tested
    without a live model). Each tool's vector is embedded once and cached by
    :func:`_doc_key`, so an unchanged tool is never re-embedded; the query is embedded
    once per call. On **any** embedding failure (model not pulled, host down, bad
    vector) the selector logs once and falls back to inline lexical ranking via
    :func:`score_tools`, so it can never crash a turn.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        embedder: Embedder,
        budget: int,
        core_extra: frozenset[str],
        core_remove: frozenset[str],
    ) -> None:
        self._registry = registry
        self._embedder = embedder
        self._budget = budget
        self._core_extra = core_extra
        self._core_remove = core_remove
        self._cache: dict[str, list[float]] = {}  # _doc_key -> tool vector
        self._warned = False  # log the embedding-failure fallback at most once

    def _core_names(self, specs: Sequence[ToolSpec]) -> set[str]:
        """Effective core set: marked-core | core_extra - core_remove."""
        return ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove

    def _tool_vector(self, spec: ToolSpec) -> list[float]:
        """Return ``spec``'s cached embedding, embedding (once) on a cache miss."""
        key = _doc_key(spec)
        vec = self._cache.get(key)
        if vec is None:
            vec = self._embedder(embed_doc(spec))
            self._cache[key] = vec
        return vec

    def _rank_gated(self, query: str, gated: Sequence[ToolSpec]) -> list[ToolSpec]:
        """Rank gated tools by cosine to ``query``; lexical fallback on any failure.

        Embeds the query once, then scores each gated tool against its cached vector.
        Zero-or-negative-similarity tools are dropped (like lexical's zero-score rule).
        Any exception from the embedder routes the whole ranking through the lexical
        fallback (warned once), so a missing model never breaks the turn.
        """
        if not gated:
            return []
        try:
            qv = self._embedder(query)
            scored = [(s, cosine(qv, self._tool_vector(s))) for s in gated]
        except Exception:  # model not pulled, host down, etc. — degrade, never crash
            if not self._warned:
                self._warned = True
                _log.warning(
                    "embedding selection failed; falling back to lexical"
                    " (is the embedding model pulled? `ollama pull`)",
                    exc_info=True,
                )
            return [s for s, _ in score_tools(query, gated)]
        ranked = sorted(scored, key=lambda p: (-p[1], p[0].name))
        return [s for s, sim in ranked if sim > 0.0]

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return core | top-K embedding-ranked gated | pinned, deduped and bounded."""
        specs = self._registry.specs()
        core_names = self._core_names(specs)
        core = [s for s in specs if s.name in core_names]
        gated = [s for s in specs if s.name not in core_names]

        k = max(0, self._budget - len(core))
        ranked = self._rank_gated(query, gated)[:k]

        pinned_specs = [s for s in specs if s.name in pinned and s.name not in core_names]

        chosen: list[ToolSpec] = []
        seen: set[str] = set()
        for s in (*core, *ranked, *pinned_specs):
            if s.name not in seen:
                seen.add(s.name)
                chosen.append(s)
        return chosen

    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Return the names of the best embedding-ranked **gated** tools for ``intent``.

        Powers Phase 2's ``find_tools`` escape hatch. Searches only the gated pool
        (core tools are already advertised every turn), degrading to lexical ranking
        on an embedding failure exactly as :meth:`select` does.
        """
        specs = self._registry.specs()
        core_names = self._core_names(specs)
        gated = [s for s in specs if s.name not in core_names]
        return [s.name for s in self._rank_gated(intent, gated)[:limit]]


def _ollama_embedder(settings: Settings) -> Embedder:
    """Build a local Ollama-backed embedder closing over one client.

    Lazy-imports the ``ollama`` client (kept out of the import path for the lexical/all
    selectors and the test suite) and embeds via the LOCAL embeddings endpoint, so
    nothing leaves the machine. Any error propagates to the caller's fallback handler.
    """
    from ollama import Client

    client = Client(host=settings.ollama_host)
    model = settings.embedding_model

    def embed(text: str) -> list[float]:
        resp = client.embed(model=model, input=text)
        return list(resp.embeddings[0])

    return embed


def build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector:
    """Construct the configured selector.

    - ``"all"`` -> :class:`AllToolsSelector` (advertise everything; debugging escape hatch).
    - ``"embedding"`` -> :class:`EmbeddingToolSelector` ranking gated tools by local
      embeddings, with inline lexical fallback on any embedding failure.
    - anything else (incl. the default ``"lexical"``) -> :class:`LexicalToolSelector`.
    """
    if settings.tool_selection == "all":
        return AllToolsSelector(registry)
    fallback = LexicalToolSelector(
        registry,
        budget=settings.tool_budget,
        core_extra=frozenset(settings.tool_core_extra),
        core_remove=frozenset(settings.tool_core_remove),
    )
    if settings.tool_selection == "embedding":
        return EmbeddingToolSelector(
            registry,
            embedder=_ollama_embedder(settings),
            budget=settings.tool_budget,
            core_extra=frozenset(settings.tool_core_extra),
            core_remove=frozenset(settings.tool_core_remove),
        )
    return fallback
