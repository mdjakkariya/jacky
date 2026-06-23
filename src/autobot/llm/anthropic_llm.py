"""Anthropic (Claude) implementation of :class:`~autobot.core.interfaces.LanguageModel`.

The optional cloud brain. Same contract as the local Ollama model
(``run_turn(user_text, execute) -> reply``): it advertises the registry's tools,
runs any tool calls **through the injected executor** (so the local permission
gate still guards every action), feeds results back, and returns the final reply.

Privacy: enabling this sends the conversation text, the injected memory profile,
and tool schemas/results to Anthropic — but never audio, and never the actions
themselves (those execute locally via the gate). It's opt-in and disclosed.

The ``anthropic`` SDK is imported lazily (optional ``cloud`` extra). The pure
helpers (tool-schema mapping, response parsing) are unit-tested with a fake
client — no network, no key.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolExecutor
from autobot.llm.ollama_llm import _SUMMARIZE_INSTRUCTION, SYSTEM_PROMPT
from autobot.logging_setup import get_logger
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from autobot.memory.store import MemoryStore

_log = get_logger("llm")

_MAX_TOOL_ROUNDS = 6  # cap the plan→tool→result loop so it can't spin forever
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
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost_usd(model: str, in_tokens: int, out_tokens: int) -> float | None:
    """Rough USD cost for a call, or None if the model's price isn't known here."""
    price = _PRICING_USD_PER_MTOK.get(model)
    if price is None:
        return None
    return in_tokens / 1_000_000 * price[0] + out_tokens / 1_000_000 * price[1]


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
        self._history: list[dict[str, Any]] = []
        # The real size of the most recent prompt we sent (input + cache read + cache
        # write), for the context meter — input_tokens alone is misleading once the
        # prefix is cached. 0 until the first turn.
        self._last_prompt_total = 0
        self._last_cache_read = 0
        self._last_cache_write = 0
        # This turn's fresh input / generated output (the incremental cost of the last
        # exchange) — for the meter's "This turn" line. 0 until the first turn.
        self._last_turn_in = 0
        self._last_turn_out = 0
        # Running summary of older turns once the context crosses compact_at — we keep
        # the recent turns verbatim and fold everything older into this (cheaper, and
        # preserves the gist instead of dropping it).
        self._summary = ""
        # Running token/cost tally for the process, surfaced in the log each turn.
        self._session_in = 0
        self._session_out = 0
        self._session_cost = 0.0
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
        _log.info(
            "cloud LLM ready model=%s context_window=%d", settings.anthropic_model, self._window
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
        turn_in: int,
        turn_out: int,
        cache_read: int = 0,
        cache_write: int = 0,
        prompt_total: int = 0,
    ) -> None:
        """Log this turn's context size, cache hit/miss, and an estimated cost.

        ``cache_read`` / ``cache_write`` are the prompt-cache tokens: a healthy
        multi-turn session shows cache_read climbing after the first turn. If
        cache_read stays 0 across turns, caching isn't working (prefix changed, or
        the prefix is under the model's minimum) — that's the signal to investigate.
        """
        if turn_in == 0 and turn_out == 0 and cache_read == 0 and cache_write == 0:
            return  # no usage reported (e.g. the request failed before any response)
        model = self._settings.anthropic_model
        self._session_in += turn_in
        self._session_out += turn_out
        cost = estimate_cost_usd(model, turn_in, turn_out)
        self._transcript.record_usage(turn_in, turn_out, cost)
        cost_str = f"{cost:.5f}" if cost is not None else "n/a"
        if cost is not None:
            self._session_cost += cost
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
            self._session_in + self._session_out,
            self._session_cost,
        )

    def _system(self) -> str:
        """System prompt + memory profile + running summary of compacted older turns."""
        parts = [SYSTEM_PROMPT]
        if self._memory is not None:
            ctx = self._memory.context()
            if ctx:
                parts.append(ctx)
        if self._summary:
            parts.append(f"Summary of earlier conversation: {self._summary}")
        return "\n\n".join(parts)

    def _summarize(self, older: list[dict[str, Any]]) -> str:
        """Fold older turns (and any prior summary) into a concise summary via Claude.

        A separate, tool-free call; on failure we keep the previous summary so a
        transient error never loses memory.
        """
        body = (f"Previous summary: {self._summary}\n\n" if self._summary else "") + "\n".join(
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
            return text_from_content(_get(resp, "content")) or self._summary
        except Exception:
            _log.warning("cloud summarization failed; keeping previous summary")
            return self._summary

    def _maybe_compact(self, prompt_total: int) -> None:
        """At the compaction threshold, summarize older turns and keep the recent ones.

        Preserves the gist (vs. dropping turns) and shrinks the prompt to a small,
        stable prefix — so the cache resets once here, then stays warm for many turns.
        """
        if not self._window or prompt_total < int(self._settings.compact_at * self._window):
            return
        kept = trim_history(self._history, _KEEP_RECENT_MESSAGES)  # clean recent tail
        older = self._history[: len(self._history) - len(kept)]
        if not older:
            return
        self._summary = self._summarize(older)
        self._history = kept
        _log.info("compacted (cloud) summarized=%d kept=%d", len(older), len(kept))

    def _history_tokens(self) -> int:
        """Rough token estimate of the conversation history (char/4, not exact)."""
        chars = 0
        for m in self._history:
            c = m.get("content")
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, list):
                for b in c:
                    chars += len(str(b.get("text") or b.get("content") or b.get("input") or ""))
        return chars // _CHARS_PER_TOKEN

    def _drop_oldest_turn(self) -> bool:
        """Remove the oldest complete turn from the front. False if only one is left."""
        if len(self._history) <= 1:
            return False
        self._history.pop(0)
        while len(self._history) > 1 and self._history[0].get("role") != "user":
            self._history.pop(0)
        return True

    def _truncate_last_user(self) -> bool:
        """Halve the newest user message's text (last resort for one giant message)."""
        for m in reversed(self._history):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                text = m["content"]
                if len(text) > 4000:
                    m["content"] = text[: len(text) // 2].rstrip() + " …[truncated]"
                    return True
                return False
        return False

    def _fit_to_budget(self, budget: int) -> None:
        """Trim oldest turns only when over ``budget``.

        Trims down to a headroom target, so we drop a chunk and leave room. This keeps
        the cached prefix stable across many turns (cache hits) instead of trimming one
        message every turn (which invalidates the cache every time).
        """
        if self._history_tokens() <= budget:
            return  # under budget: don't touch history, keep the cache warm
        target = int(budget * _TRIM_HEADROOM)
        while self._history_tokens() > target and self._drop_oldest_turn():
            pass

    def _budget(self, overhead: int) -> int:
        """Token budget for history = window - reserved output - system/tools - safety.

        Reads ``self._window`` live so a window learned from an error tightens the
        budget on the very next attempt.
        """
        return max(
            8_000, self._window - self._settings.anthropic_max_tokens - overhead - _CONTEXT_SAFETY
        )

    def _send(self, tools: list[dict[str, Any]], overhead: int) -> Any:
        """Create one message, trimming to fit the (dynamic) window.

        Retries on a 'too long' rejection (learning the real window from it, so any
        model is handled). Raises for any other error, or if it can't be made to fit.
        """
        truncated = False
        for _ in range(8):
            self._fit_to_budget(self._budget(overhead))
            try:
                return self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.anthropic_max_tokens,
                    temperature=self._settings.llm_temperature,
                    system=self._system(),
                    messages=with_cache_breakpoint(self._history),
                    tools=tools,
                )
            except Exception as exc:
                if not is_too_long_error(exc):
                    raise
                limit = parse_window_limit(exc)
                if limit and limit < self._window:
                    _log.info("cloud context window learned=%d (was %d)", limit, self._window)
                    self._window = limit  # self-correct: tightens _budget next loop
                if self._drop_oldest_turn():
                    _log.warning("cloud prompt too long — dropped oldest turn, retrying")
                    continue
                if not truncated and self._truncate_last_user():
                    truncated = True
                    _log.warning("cloud prompt too long — truncated newest message, retrying")
                    continue
                raise
        raise RuntimeError("prompt too long after trimming")

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one turn end-to-end; tool calls run through ``execute`` (the gate).

        Keeps an **append-only** full history (incl. ``tool_use`` / ``tool_result``
        blocks) so the model has a faithful record of what it did, and so the prefix
        stays byte-stable for prompt caching. The history is trimmed to fit the context
        window *before* sending (and on a 'too long' rejection it trims more and
        retries), so a long session can't get permanently stuck. A cache breakpoint on
        the last block caches tools + system + prior turns; per-turn we log usage.
        """
        tools = to_anthropic_tools(self._registry.schemas())
        overhead = (len(self._system()) + sum(len(str(t)) for t in tools)) // _CHARS_PER_TOKEN
        # Append-only: everything for this turn is added to the live history. On a
        # request failure we roll back to here so a half-built turn can't corrupt it.
        start = len(self._history)
        self._history.append({"role": "user", "content": user_text})

        reply = ""
        turn_in = turn_out = cache_read = cache_write = prompt_total = 0
        for _ in range(_MAX_TOOL_ROUNDS):
            problem = _first_pairing_problem(self._history)
            if problem:  # should never happen — but if it does, say exactly where
                _log.error("history integrity broken before send: %s", problem)
            try:
                resp = self._send(tools, overhead)
            except Exception as exc:  # cloud rejected/unreachable — stay useful
                _log.warning("cloud request failed: %s", exc)
                del self._history[start:]  # abandon this turn; keep history valid
                return too_long_reply() if is_too_long_error(exc) else cloud_error_reply(exc)
            usage = _get(resp, "usage")
            in_tok = int(_get(usage, "input_tokens") or 0)
            cr = int(_get(usage, "cache_read_input_tokens") or 0)
            cw = int(_get(usage, "cache_creation_input_tokens") or 0)
            turn_in += in_tok
            turn_out += int(_get(usage, "output_tokens") or 0)
            cache_read += cr
            cache_write += cw
            prompt_total = in_tok + cr + cw  # the real size of this call's prompt
            content = _get(resp, "content") or []
            # Record the assistant turn (text and/or tool_use blocks) faithfully.
            self._history.append(
                {"role": "assistant", "content": [_block_to_dict(b) for b in content]}
            )
            calls = parse_tool_uses(content)
            if not calls:
                reply = text_from_content(content)
                break
            _log.info("planned tools=%s (cloud)", [c.name for c in calls])
            results: list[dict[str, Any]] = []
            for block in content:
                if _get(block, "type") != "tool_use":
                    continue
                call = ToolCall(
                    name=_get(block, "name"),
                    arguments=_get(block, "input")
                    if isinstance(_get(block, "input"), dict)
                    else {},
                )
                result = execute(call)  # through the local permission gate
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": _get(block, "id"),
                        "content": result.content,
                    }
                )
            self._history.append({"role": "user", "content": results})
        else:
            reply = reply or "Sorry, that took too many steps."

        self._last_prompt_total = prompt_total
        self._last_cache_read = cache_read
        self._last_cache_write = cache_write
        # "This turn in" = freshly-processed input (uncached input + newly cached
        # tokens), NOT input_tokens alone — once the prefix is cached, input_tokens
        # collapses to a handful while cache_write holds the real new content (e.g. a
        # big paste). cache_read is excluded (it's the cheap, already-seen prefix).
        self._last_turn_in = turn_in + cache_write
        self._last_turn_out = turn_out
        self._log_usage(turn_in, turn_out, cache_read, cache_write, prompt_total)
        # Summarize older turns once we cross the threshold (keeps the gist, shrinks
        # the prompt to a small stable prefix); a hard message cap is the last backstop.
        self._maybe_compact(prompt_total)
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)
        return reply

    def new_session(self) -> None:
        """Discard all conversation history and start a fresh session.

        Clears the running summary and per-turn usage trackers too, so the context
        meter resets to empty. The model config (window, client) is untouched — only
        the conversation is wiped. Drives the chat's "New chat" action.
        """
        self._history = []
        self._summary = ""
        self._last_prompt_total = 0
        self._last_cache_read = 0
        self._last_cache_write = 0
        self._last_turn_in = 0
        self._last_turn_out = 0
        _log.info("cloud session reset (new chat)")

    @property
    def context_window(self) -> int:
        """The model's prompt-token window (dynamic; for the context meter)."""
        return self._window

    def context_usage(self) -> dict[str, Any] | None:
        """Context-meter payload for this session, or None before the first turn."""
        if not self._last_prompt_total:
            return None
        return {
            "used": self._last_prompt_total,
            "window": self._window,
            "cache_read": self._last_cache_read,
            "cache_write": self._last_cache_write,
            "turn_in": self._last_turn_in,
            "turn_out": self._last_turn_out,
            "model": self._settings.anthropic_model,
        }

    @property
    def last_prompt_tokens(self) -> int:
        """Real size of the most recent prompt (input + cache read + cache write)."""
        return self._last_prompt_total


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
