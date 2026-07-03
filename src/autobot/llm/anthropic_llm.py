"""Anthropic (Claude) implementation of :class:`~autobot.agent.chat_model.ChatModel`.

The optional cloud brain. Implements the same provider-agnostic turn primitives
as the local Ollama model (``begin_turn``/``send``/``handle_discovery``/
``record_results``/``final_answer_no_tools``/``finalize_turn``); the shared
:class:`~autobot.agent.harness.AgentHarness` drives the tool-round loop, running
any tool calls **through the injected executor** (so the local permission gate
still guards every action), feeding results back, and returning the final reply.

Privacy: enabling this sends the conversation text, the injected memory profile,
and tool schemas/results to Anthropic — but never audio, and never the actions
themselves (those execute locally via the gate). It's opt-in and disclosed.

The ``anthropic`` SDK is imported lazily (optional ``cloud`` extra). The pure
helpers (tool-schema mapping, response parsing) are unit-tested with a fake
client — no network, no key.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from autobot.agent.chat_model import ChatResponse
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.ollama_llm import _SUMMARIZE_INSTRUCTION, system_prompt
from autobot.logging_setup import get_logger
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from autobot.agent.session import Session
    from autobot.memory.store import MemoryStore

_log = get_logger("llm")

_HARD_MAX_MESSAGES = 400  # absolute backstop on list growth; the token budget governs first
_TRIM_HEADROOM = 0.8  # when we must trim, trim down to this fraction of budget — so we
# trim in chunks (not one message per turn), keeping the cached prefix stable for many
# turns instead of invalidating the cache every turn.
_KEEP_RECENT_MESSAGES = 20  # ≈ last 10 turns kept verbatim when we compact (summarize)
_CONTEXT_SAFETY = 6_000  # headroom for our rough char/4 estimate being under the real count
_CHARS_PER_TOKEN = 4  # rough English ratio for the budget estimate (not exact tokenization)

# Prompt windows are per-model and change over time, so we don't hardcode one. The
# window is resolved as: settings override → this per-model default (prefix match)
# → a safe fallback — and then *self-corrected* from the API's "… > N maximum"
# rejection, so a smaller (or unknown) model fixes itself after one trimmed turn.
_DEFAULT_CLOUD_WINDOW = 200_000
# Per-model defaults (prefix match, most-specific first). Used only as a fallback —
# at startup we ask the Models API for the real limit (see _resolve_window).
_MODEL_WINDOWS: dict[str, int] = {
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
}
_MAX_TOKENS_RE = re.compile(r">\s*([0-9]+)\s*maximum")

# Anthropic server-side Tool Search Tool (verified against SDK 0.109.2 —
# ToolSearchToolBm25_20251119Param is in the standard messages.create tools union,
# so no beta header is needed). The model discovers deferred tools by searching this
# tool; deferred tool defs cost ~0 baseline tokens until loaded on demand.
TOOL_SEARCH_TYPE = "tool_search_tool_bm25_20251119"
TOOL_SEARCH_NAME = "tool_search_tool_bm25"
# Models for which "auto" turns ON native tool search. All listed models support it
# (Sonnet 4+, Opus 4+, Haiku 4.5+). The default model claude-haiku-4-5 is included so the
# out-of-the-box cloud experience bounds context: deferred MCP tools cost ~0 baseline and
# the model selects among a relevant few rather than the whole catalog. The tradeoff is a
# small per-use cost (the server search + tool definitions loaded on demand as fresh,
# uncached input) — now visible in the cost meter, which prices cache reads/writes. Set
# anthropic_tool_search="off" to fall back to advertising all tools (cheapest on short
# turns with a static, cache-friendly tool set); "on" forces search for any model.
_TOOL_SEARCH_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)


def default_window_for(model: str) -> int:
    """The prompt window for a model: a known per-model value, else a safe default."""
    for prefix, window in _MODEL_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return _DEFAULT_CLOUD_WINDOW


def parse_window_limit(exc: Exception) -> int | None:
    """Pull the real maximum out of a 'prompt is too long: X > N maximum' error."""
    match = _MAX_TOKENS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def is_too_long_error(exc: Exception) -> bool:
    """True if the cloud rejected the request because the prompt exceeded the window."""
    s = str(exc).lower()
    return "prompt is too long" in s or "too many tokens" in s or "context window" in s


def too_long_reply() -> str:
    """Calm reply when even after trimming the turn won't fit (e.g. one giant message)."""
    return (
        "That was a lot to take in — I trimmed the older parts of our chat to keep going. "
        "Try again, or start a fresh chat if you'd like a clean slate."
    )


# Approximate list prices in USD per million tokens (input, output), for a *cost
# estimate* in the log — this is debugging signal, not billing. Add/adjust entries
# as Anthropic's pricing changes; unknown models simply log tokens without a cost.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Keyed by model-id PREFIX so 4.x point releases match. Approximate list prices.
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4": (5.0, 25.0),
}
# Prompt-cache multipliers applied to the base INPUT rate (Anthropic): a 5-minute cache
# write costs 1.25x, a cache read 0.1x (90% off). Pricing these matters because a large
# static tool prefix is almost entirely cache_read/cache_write — omitting them made
# "advertise all tools" look free while tool search's on-demand (uncached) tool loads
# looked expensive, which is exactly backwards from real billing.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1


def estimate_cost_usd(
    model: str,
    in_tokens: int,
    out_tokens: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Rough USD cost for a call, or None if the model's price isn't known here.

    Prices fresh input and output at the model's list rate, plus prompt-cache tokens at
    Anthropic's multipliers on the input rate (write 1.25x, read 0.1x). ``cache_read`` /
    ``cache_write`` default to 0 so existing callers are unaffected; the daemon passes
    them so the logged session cost reflects real billing rather than fresh tokens alone.
    """
    price = next(
        (p for prefix, p in _PRICING_USD_PER_MTOK.items() if model.startswith(prefix)), None
    )
    if price is None:
        return None
    in_rate, out_rate = price
    return (
        in_tokens / 1_000_000 * in_rate
        + cache_write / 1_000_000 * in_rate * _CACHE_WRITE_MULT
        + cache_read / 1_000_000 * in_rate * _CACHE_READ_MULT
        + out_tokens / 1_000_000 * out_rate
    )


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a dict or as an attribute (SDK blocks are objects)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def to_anthropic_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map our OpenAI-style tool schemas to Anthropic's ``input_schema`` shape."""
    tools: list[dict[str, Any]] = []
    for schema in schemas:
        fn = schema.get("function", {})
        if not fn.get("name"):
            continue
        tools.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return tools


def tool_search_supported(model: str, mode: str) -> bool:
    """Whether to use Anthropic's native Tool Search Tool for ``model``.

    Resolves the ``anthropic_tool_search`` setting: ``"off"`` never uses it,
    ``"on"`` always does (to try a model not yet in the table), and ``"auto"``
    enables it only for models known to support it (prefix match against
    :data:`_TOOL_SEARCH_MODEL_PREFIXES`). Unknown ``mode`` is treated as ``"auto"``.
    """
    if mode == "off":
        return False
    if mode == "on":
        return True
    return any(model.startswith(p) for p in _TOOL_SEARCH_MODEL_PREFIXES)


def partition_tools(specs: Sequence[ToolSpec]) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """Split specs into ``(core, gated)`` by :attr:`ToolSpec.core`.

    Core tools are advertised to the cloud model normally; gated tools are deferred
    (``defer_loading``) and discovered via tool search. This is the only place the
    cloud path reads the tiering flag — it deliberately does not import the local
    ``tools.selection`` module (the cloud path uses native server-side search, not the
    client-side lexical selector).
    """
    core = [s for s in specs if s.core]
    gated = [s for s in specs if not s.core]
    return core, gated


def _tool_param(spec: ToolSpec) -> dict[str, Any]:
    """One Anthropic tool entry from a ToolSpec (without defer_loading / cache_control)."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters or {"type": "object", "properties": {}},
    }


def assemble_anthropic_tools(
    specs: Sequence[ToolSpec],
    *,
    tool_search: bool,
    relevant: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Build the cloud ``tools`` payload: relevance-tiered + search + cached, or all tools.

    When ``tool_search`` is ``False`` (disabled, or an unsupported model), returns every
    tool exactly as before — the byte-for-byte legacy request. When ``True``: core tools
    are advertised normally; gated tools whose name is in ``relevant`` (those matching
    this turn's message) are ALSO advertised directly so the model can actually pick
    them; the rest get ``defer_loading: True`` and are discovered on demand via the
    appended, non-deferred Tool Search Tool. A ``cache_control`` ephemeral breakpoint is
    stamped on the last tool.

    Surfacing the relevant gated tools directly is the fix for deferred-only MCP tools
    being invisible — which made the model fall back to a visible core tool (e.g. opening
    a website) instead of using the MCP tool. The long tail stays deferred (~0 baseline
    tokens) with the search tool as the recall net.
    """
    if not tool_search:
        return to_anthropic_tools([s.to_schema() for s in specs])
    core, gated = partition_tools(specs)
    tools: list[dict[str, Any]] = [_tool_param(s) for s in core]
    for s in gated:
        param = _tool_param(s)
        if s.name not in relevant:
            param["defer_loading"] = True  # invisible until discovered via tool search
        tools.append(param)
    tools.append({"type": TOOL_SEARCH_TYPE, "name": TOOL_SEARCH_NAME})
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def parse_tool_uses(content: Any) -> list[ToolCall]:
    """Extract ``tool_use`` blocks from a response's content as typed ToolCalls."""
    calls: list[ToolCall] = []
    for block in content or []:
        if _get(block, "type") == "tool_use" and _get(block, "name"):
            args = _get(block, "input")
            calls.append(
                ToolCall(name=_get(block, "name"), arguments=args if isinstance(args, dict) else {})
            )
    return calls


def text_from_content(content: Any) -> str:
    """Join the text blocks of a response into the spoken reply."""
    parts = [_get(b, "text") or "" for b in (content or []) if _get(b, "type") == "text"]
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def cloud_error_reply(_exc: Exception) -> str:
    """A short, calm spoken reply when the cloud model can't be reached.

    The real cause (usage limit, bad key, outage) is logged by the caller; we
    deliberately do **not** speak the raw API error — read aloud it's long, noisy,
    and unhelpful. The user just needs to know it's temporary and what to do now.
    """
    return (
        "The cloud model isn't responding right now. You can try again in a little "
        "while, or switch to the local model in Settings for an immediate response."
    )


def _text_of(message: dict[str, Any]) -> str:
    """Plain-text rendering of a history message for summarization input."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content or []:
        kind = _get(b, "type")
        if kind == "text":
            parts.append(_get(b, "text") or "")
        elif kind == "tool_use":
            parts.append(f"[called {_get(b, 'name')} {_get(b, 'input') or {}}]")
        elif kind == "tool_result":
            parts.append(f"[tool result: {_get(b, 'content')}]")
    return " ".join(p for p in parts if p)


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Reconstruct an assistant content block as a dict to send back to the API."""
    kind = _get(block, "type")
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": _get(block, "id"),
            "name": _get(block, "name"),
            "input": _get(block, "input") or {},
        }
    return {"type": "text", "text": _get(block, "text") or ""}


def with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy ``messages`` with an ephemeral cache breakpoint on the very last block.

    Anthropic caches a *prefix*: one breakpoint caches everything before it
    (tools → system → all prior messages). Putting it on the final block caches the
    whole stable conversation so the next call reads it instead of re-billing it.
    The history itself is left clean (no cache_control persisted) so trimming and
    re-sending stay byte-stable — which is what keeps the cache valid.
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    blocks: list[dict[str, Any]] = (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else [dict(b) for b in (content or [])]
    )
    if blocks:
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    return [*messages[:-1], last]


def trim_history(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep the most recent messages, but never start mid tool-exchange.

    A leading orphaned ``tool_result`` (or an assistant ``tool_use`` whose result was
    cut) makes the API reject the request, so we drop forward to the next plain user
    turn. Also keeps the cached prefix stable from a clean boundary.
    """
    if len(history) <= limit:
        return history
    trimmed = history[-limit:]
    while trimmed and not (
        trimmed[0].get("role") == "user" and isinstance(trimmed[0].get("content"), str)
    ):
        trimmed.pop(0)
    return trimmed


def _first_pairing_problem(messages: list[dict[str, Any]]) -> str | None:
    """Return a description of the first tool_use/tool_result pairing fault, else None.

    The API rejects a conversation where an assistant ``tool_use`` isn't answered by
    a matching ``tool_result`` in the very next message (or a ``tool_result`` appears
    with no preceding ``tool_use``). We check our own history *before* sending so a
    corruption is reported with the exact index — turning an opaque 400 / silent
    context loss into a precise, debuggable log line.
    """
    pending: set[Any] | None = None  # tool_use ids that the next message must answer
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        results = {_get(b, "tool_use_id") for b in blocks if _get(b, "type") == "tool_result"}
        uses = {_get(b, "id") for b in blocks if _get(b, "type") == "tool_use"}
        if pending is not None:
            if role != "user" or not pending.issubset(results):
                return f"message[{i}] role={role} did not answer tool_use id(s) {sorted(pending)}"
            pending = None
        elif results:
            return f"message[{i}] role={role} has tool_result with no preceding tool_use"
        if uses:
            if role != "assistant":
                return f"message[{i}] role={role} has tool_use (expected assistant)"
            pending = uses
    if pending:
        return f"history ends with an unanswered tool_use {sorted(pending)}"
    return None


class AnthropicLanguageModel:
    """Runs user turns against Anthropic Claude with tool calling."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        # Per-turn buffers shared by the ChatModel primitives. Valid only during one
        # serialized turn; re-initialized in begin_turn from the passed session.
        self._turn_start = 0  # index in session.history where this turn began (rollback point)
        self._tools: list[dict[str, Any]] = []  # this turn's assembled tools payload
        self._overhead = 0  # estimated system+tools token overhead this turn
        self._turn_in = 0
        self._turn_out = 0
        self._cache_read = 0
        self._cache_write = 0
        self._prompt_total = 0
        self._turn_failed = False  # a send failed this turn -> loop should end
        self._turn_error = ""  # the reply to return when a send failed
        self._last_content: Any = []  # last assistant response's content blocks
        if client is not None:  # injected (tests)
            self._client = client
        else:
            import anthropic

            key = api_key or _require_key()
            self._client = anthropic.Anthropic(api_key=key)
        # Per-model prompt window (not hardcoded): explicit override → live Models-API
        # lookup → per-model default; self-corrected downward if the API ever rejects
        # an over-long prompt. So 200k, 1M, or a smaller model all just work. Needs
        # self._client, so resolve after it's set.
        self._window = self._resolve_window()
        # Gate on model support (auto), or honor an explicit on/off. Pure decision —
        # never raises — so a misconfigured flag degrades to advertise-all, never a
        # startup crash. Logged as a bool only (no tokens/secrets).
        self._tool_search = tool_search_supported(
            settings.anthropic_model, settings.anthropic_tool_search
        )
        _log.info(
            "cloud LLM ready model=%s context_window=%d tool_search=%s",
            settings.anthropic_model,
            self._window,
            self._tool_search,
        )

    def _resolve_window(self) -> int:
        """Resolve the model's prompt window: settings override → Models API → default.

        The Models API (``/v1/models``) reports each model's real limit, so new or
        larger-context models work without code changes; any lookup failure falls
        back to the per-model default. Self-correction from a 'too long' error is the
        final safety net (see :meth:`_send`).
        """
        if self._settings.anthropic_context_tokens:
            return self._settings.anthropic_context_tokens
        try:
            info = self._client.models.retrieve(self._settings.anthropic_model)
            for attr in ("max_input_tokens", "context_window", "max_context_tokens"):
                value = _get(info, attr)
                if isinstance(value, int) and value > 0:
                    return value
        except Exception as exc:  # offline, old SDK, unknown field — use the default
            _log.debug("models API window lookup failed (%s); using default", exc)
        return default_window_for(self._settings.anthropic_model)

    def _log_usage(
        self,
        session: Session,
        turn_in: int,
        turn_out: int,
        cache_read: int = 0,
        cache_write: int = 0,
        prompt_total: int = 0,
    ) -> None:
        """Log this turn's context size, cache hit/miss, and an estimated cost.

        Updates ``session.cost`` (the running per-session tally). ``cache_read`` /
        ``cache_write`` are the prompt-cache tokens: a healthy multi-turn session shows
        cache_read climbing after the first turn. If cache_read stays 0 across turns,
        caching isn't working (prefix changed, or the prefix is under the model's
        minimum) — that's the signal to investigate.
        """
        if turn_in == 0 and turn_out == 0 and cache_read == 0 and cache_write == 0:
            return  # no usage reported (e.g. the request failed before any response)
        model = self._settings.anthropic_model
        session.cost.in_tokens += turn_in
        session.cost.out_tokens += turn_out
        cost = estimate_cost_usd(
            model, turn_in, turn_out, cache_read=cache_read, cache_write=cache_write
        )
        self._transcript.record_usage(turn_in, turn_out, cost)
        cost_str = f"{cost:.5f}" if cost is not None else "n/a"
        if cost is not None:
            session.cost.usd += cost
            session.cost.priced = True
        pct = round(100 * prompt_total / self._window) if prompt_total and self._window else 0
        _log.info(
            "cloud usage model=%s input_tokens=%d output_tokens=%d cache_read=%d cache_write=%d "
            "prompt_total=%d ctx_pct=%d est_cost_usd=%s session_tokens=%d session_cost_usd=%.5f",
            model,
            turn_in,
            turn_out,
            cache_read,
            cache_write,
            prompt_total,
            pct,
            cost_str,
            session.cost.in_tokens + session.cost.out_tokens,
            session.cost.usd,
        )

    def _system(self, session: Session) -> str:
        """System prompt + memory profile + running summary of compacted older turns."""
        parts = [system_prompt(session.delivery_mode)]
        if self._memory is not None:
            ctx = self._memory.context()
            if ctx:
                parts.append(ctx)
        from autobot.llm.ollama_llm import active_folder_line, meeting_state_line

        folder = active_folder_line()
        if folder:
            parts.append(folder)
        meeting = meeting_state_line()
        if meeting:
            parts.append(meeting)
        if session.summary:
            parts.append(f"Summary of earlier conversation: {session.summary}")
        return "\n\n".join(parts)

    def _summarize(self, session: Session, older: list[dict[str, Any]]) -> str:
        """Fold older turns (and any prior summary) into a concise summary via Claude.

        A separate, tool-free call; on failure we keep the previous summary so a
        transient error never loses memory.
        """
        body = (f"Previous summary: {session.summary}\n\n" if session.summary else "") + "\n".join(
            f"{m.get('role', '?')}: {_text_of(m)}" for m in older
        )
        try:
            resp = self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.anthropic_max_tokens,
                temperature=self._settings.llm_temperature,
                system=_SUMMARIZE_INSTRUCTION,
                messages=[{"role": "user", "content": body}],
            )
            return text_from_content(_get(resp, "content")) or session.summary
        except Exception:
            _log.warning("cloud summarization failed; keeping previous summary")
            return session.summary

    def _maybe_compact(self, session: Session, prompt_total: int) -> None:
        """At the compaction threshold, summarize older turns and keep the recent ones.

        Preserves the gist (vs. dropping turns) and shrinks the prompt to a small,
        stable prefix — so the cache resets once here, then stays warm for many turns.
        """
        if not self._window or prompt_total < int(self._settings.compact_at * self._window):
            return
        kept = trim_history(session.history, _KEEP_RECENT_MESSAGES)  # clean recent tail
        older = session.history[: len(session.history) - len(kept)]
        if not older:
            return
        session.summary = self._summarize(session, older)
        session.history = kept
        _log.info("compacted (cloud) summarized=%d kept=%d", len(older), len(kept))

    def _history_tokens(self, session: Session) -> int:
        """Rough token estimate of the conversation history (char/4, not exact)."""
        chars = 0
        for m in session.history:
            c = m.get("content")
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, list):
                for b in c:
                    chars += len(str(b.get("text") or b.get("content") or b.get("input") or ""))
        return chars // _CHARS_PER_TOKEN

    def _drop_oldest_turn(self, session: Session) -> bool:
        """Remove the oldest complete turn from the front. False if only one is left."""
        if len(session.history) <= 1:
            return False
        session.history.pop(0)
        while len(session.history) > 1 and session.history[0].get("role") != "user":
            session.history.pop(0)
        return True

    def _truncate_last_user(self, session: Session) -> bool:
        """Halve the newest user message's text (last resort for one giant message)."""
        for m in reversed(session.history):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                text = m["content"]
                if len(text) > 4000:
                    m["content"] = text[: len(text) // 2].rstrip() + " …[truncated]"
                    return True
                return False
        return False

    def _fit_to_budget(self, session: Session, budget: int) -> None:
        """Trim oldest turns only when over ``budget``.

        Trims down to a headroom target, so we drop a chunk and leave room. This keeps
        the cached prefix stable across many turns (cache hits) instead of trimming one
        message every turn (which invalidates the cache every time).
        """
        if self._history_tokens(session) <= budget:
            return  # under budget: don't touch history, keep the cache warm
        target = int(budget * _TRIM_HEADROOM)
        while self._history_tokens(session) > target and self._drop_oldest_turn(session):
            pass

    def _budget(self, overhead: int) -> int:
        """Token budget for history = window - reserved output - system/tools - safety.

        Reads ``self._window`` live so a window learned from an error tightens the
        budget on the very next attempt.
        """
        return max(
            8_000, self._window - self._settings.anthropic_max_tokens - overhead - _CONTEXT_SAFETY
        )

    def _send(self, session: Session, tools: list[dict[str, Any]], overhead: int) -> Any:
        """Create one message, trimming to fit the (dynamic) window.

        Retries on a 'too long' rejection (learning the real window from it, so any
        model is handled). Raises for any other error, or if it can't be made to fit.
        """
        truncated = False
        for _ in range(8):
            self._fit_to_budget(session, self._budget(overhead))
            try:
                return self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.anthropic_max_tokens,
                    temperature=self._settings.llm_temperature,
                    system=self._system(session),
                    messages=with_cache_breakpoint(session.history),
                    tools=tools,
                )
            except Exception as exc:
                if not is_too_long_error(exc):
                    raise
                limit = parse_window_limit(exc)
                if limit and limit < self._window:
                    _log.info("cloud context window learned=%d (was %d)", limit, self._window)
                    self._window = limit  # self-correct: tightens _budget next loop
                if self._drop_oldest_turn(session):
                    _log.warning("cloud prompt too long — dropped oldest turn, retrying")
                    continue
                if not truncated and self._truncate_last_user(session):
                    truncated = True
                    _log.warning("cloud prompt too long — truncated newest message, retrying")
                    continue
                raise
        raise RuntimeError("prompt too long after trimming")

    def _assemble_tools(self, query: str) -> list[dict[str, Any]]:
        """Tools to advertise this turn: relevance-tiered + tool-search + cached, or all.

        Reads the live registry, so MCP tools that connect/disconnect between turns are
        reflected. When tool search is supported (see :meth:`__init__`), the gated tools
        whose name/description matches ``query`` are surfaced directly (un-deferred) via
        the same on-device lexical gate the local path uses — so the model actually picks
        the relevant MCP tool instead of a visible core tool — while the long tail stays
        deferred behind the search tool. Otherwise every tool is advertised (legacy).
        """
        relevant: frozenset[str] = frozenset()
        if self._tool_search:
            relevant = self._relevant_gated(query)
        return assemble_anthropic_tools(
            self._registry.specs(), tool_search=self._tool_search, relevant=relevant
        )

    def _relevant_gated(self, query: str) -> frozenset[str]:
        """Gated tool names to advertise un-deferred this turn.

        Two sources, kept deliberately small so the model isn't flooded (advertising
        too many tools degrades selection as badly as hiding the needed one):

        - the top ``tool_relevant_limit`` gated tools lexically relevant to ``query``
          (with light stemming, so "repo" surfaces ``search_repositories``), and
        - every **identity anchor** (``get_me``/"authenticated user" read tools), always,
          so a first-person request ("my repos") can resolve the account without the
          model having to discover an identity tool first.

        The long tail stays deferred behind the native tool-search tool.
        """
        from autobot.tools.selection import LexicalToolSelector, identity_tool_names

        selector = LexicalToolSelector(
            self._registry,
            budget=self._settings.tool_budget,
            core_extra=frozenset(self._settings.tool_core_extra),
            core_remove=frozenset(self._settings.tool_core_remove),
        )
        relevant = set(selector.search(query, limit=self._settings.tool_relevant_limit))
        relevant |= identity_tool_names(self._registry.specs())
        return frozenset(relevant)

    def begin_turn(self, session: Session, user_text: str) -> None:
        """Start a turn: assemble tools, append the user message, reset counters."""
        self._tools = self._assemble_tools(user_text)
        self._overhead = (
            len(self._system(session)) + sum(len(str(t)) for t in self._tools)
        ) // _CHARS_PER_TOKEN
        self._turn_start = len(session.history)
        session.history.append({"role": "user", "content": user_text})
        self._turn_in = self._turn_out = self._cache_read = self._cache_write = 0
        self._prompt_total = 0
        self._turn_failed = False
        self._turn_error = ""

    def send(self, session: Session) -> ChatResponse:
        """Send once; record the assistant blocks; return text + tool calls.

        On a cloud failure the turn is abandoned (history rolled back to the start)
        and the stashed error reply is returned directly as the response text (with
        no tool calls), so the harness's round loop breaks and surfaces it as-is.
        """
        problem = _first_pairing_problem(session.history)
        if problem:
            _log.error("history integrity broken before send: %s", problem)
        try:
            resp = self._send(session, self._tools, self._overhead)
        except Exception as exc:  # cloud rejected/unreachable — stay useful
            _log.warning("cloud request failed: %s", exc)
            del session.history[self._turn_start :]  # abandon this turn; keep history valid
            self._turn_failed = True
            self._turn_error = (
                too_long_reply() if is_too_long_error(exc) else cloud_error_reply(exc)
            )
            return ChatResponse(text=self._turn_error, tool_calls=[])
        usage = _get(resp, "usage")
        in_tok = int(_get(usage, "input_tokens") or 0)
        cr = int(_get(usage, "cache_read_input_tokens") or 0)
        cw = int(_get(usage, "cache_creation_input_tokens") or 0)
        self._turn_in += in_tok
        self._turn_out += int(_get(usage, "output_tokens") or 0)
        self._cache_read += cr
        self._cache_write += cw
        self._prompt_total = in_tok + cr + cw
        content = _get(resp, "content") or []
        session.history.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content]}
        )
        self._last_content = content  # kept so record_results can pair by block id
        calls = parse_tool_uses(content)
        return ChatResponse(text=text_from_content(content), tool_calls=calls)

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        """Anthropic discovers tools server-side (Tool Search Tool), so never inline."""
        return None

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append a user message of tool_results, paired to the last tool_use ids by order."""
        use_ids = [
            _get(b, "id") for b in (self._last_content or []) if _get(b, "type") == "tool_use"
        ]
        blocks = [
            {"type": "tool_result", "tool_use_id": uid, "content": res.content}
            for uid, (_call, res) in zip(use_ids, results, strict=False)
        ]
        session.history.append({"role": "user", "content": blocks})

    def final_answer_no_tools(self, session: Session) -> str:
        """One tools-disabled call to synthesize a final reply when the round cap is hit.

        The history ends with the last round's tool_results (a complete pairing), so
        a tool-free request yields a clean final reply. Appends the assistant message
        so the history stays faithful. Falls back to a short line on failure.
        """
        _log.info("cloud tool-round cap reached; forcing a final answer without tools")
        try:
            resp = self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.anthropic_max_tokens,
                temperature=self._settings.llm_temperature,
                system=self._system(session),
                messages=with_cache_breakpoint(session.history),
            )
        except Exception:
            _log.warning("cloud forced final answer failed")
            return "Sorry, that took too many steps."
        content = _get(resp, "content") or []
        session.history.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content]}
        )
        return text_from_content(content) or "Sorry, that took too many steps."

    def finalize_turn(self, session: Session) -> list[dict[str, Any]]:
        """Record usage, compact if over threshold, trim to the hard backstop.

        Returns:
            This turn's new history entries, for the harness to persist to the
            transcript. Empty if the turn failed (its history was rolled back).
        """
        # A failed send already rolled back this turn's history and returned the
        # error reply; skip the post-turn tail so the context meter keeps the last
        # successful turn's value (matches the pre-harness run_turn early-return).
        if self._turn_failed:
            return []
        # Capture before _maybe_compact/trim_history run below — both may reassign
        # session.history to a SHORTER list, so this must happen first.
        new = list(session.history[self._turn_start :])
        # "This turn in" = freshly-processed input (uncached input + newly cached
        # tokens), NOT input_tokens alone — once the prefix is cached, input_tokens
        # collapses to a handful while cache_write holds the real new content (e.g. a
        # big paste). cache_read is excluded (it's the cheap, already-seen prefix).
        last_turn_in = self._turn_in + self._cache_write
        last_turn_out = self._turn_out
        self._log_usage(
            session,
            self._turn_in,
            self._turn_out,
            self._cache_read,
            self._cache_write,
            self._prompt_total,
        )
        # Summarize older turns once we cross the threshold (keeps the gist, shrinks
        # the prompt to a small stable prefix); a hard message cap is the last backstop.
        self._maybe_compact(session, self._prompt_total)
        session.history = trim_history(session.history, _HARD_MAX_MESSAGES)
        if not self._prompt_total or not self._window:
            session.last_usage = None
            return new
        session.last_usage = {
            "used": self._prompt_total,
            "window": self._window,
            "cache_read": self._cache_read,
            "cache_write": self._cache_write,
            "turn_in": last_turn_in,
            "turn_out": last_turn_out,
            "model": self._settings.anthropic_model,
            # Estimated session cost in USD; None when this model has no list price
            # (the UI then hides the cost row instead of showing a misleading $0.00).
            "price": session.cost.usd if session.cost.priced else None,
        }
        return new

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion via the Anthropic Messages API (no tools).

        A single non-conversational call — no history, no tools, no executor. Used by
        the meeting summarizer and similar batch tasks that need a plain LLM completion
        rather than a full interactive turn.

        Args:
            prompt: The full prompt to send.
            temperature: Sampling temperature; 0.0 for deterministic output.

        Returns:
            The model's reply text, stripped of leading/trailing whitespace.
        """
        resp = self._client.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=self._settings.anthropic_max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            _get(block, "text") or ""
            for block in (_get(resp, "content") or [])
            if _get(block, "type") == "text"
        ).strip()

    @property
    def context_window(self) -> int:
        """The model's prompt-token window (dynamic; for the context meter)."""
        return self._window


def _require_key() -> str:
    """Fetch the Anthropic key from the Keychain, or fail with a clear message."""
    from autobot.secrets import get_secret

    key = get_secret("anthropic_api_key")
    if not key:
        raise ValueError(
            "Cloud mode needs an Anthropic API key. Add it in the Settings view, "
            "or store it: security add-generic-password -U -s autobot -a "
            "anthropic_api_key -w 'YOUR-KEY'"
        )
    return key
