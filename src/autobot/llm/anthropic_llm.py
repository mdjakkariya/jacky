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

from typing import TYPE_CHECKING, Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolExecutor
from autobot.llm.ollama_llm import SYSTEM_PROMPT
from autobot.logging_setup import get_logger
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from autobot.memory.store import MemoryStore

_log = get_logger("llm")

_MAX_TOOL_ROUNDS = 6  # cap the plan→tool→result loop so it can't spin forever
_HARD_MAX_MESSAGES = 100

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
    blocks = (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else [dict(b) for b in content]
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
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
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
        _log.info("cloud LLM ready model=%s", settings.anthropic_model)

    def _log_usage(
        self, turn_in: int, turn_out: int, cache_read: int = 0, cache_write: int = 0
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
        _log.info(
            "cloud usage model=%s context_tokens=%d output_tokens=%d "
            "cache_read=%d cache_write=%d est_cost_usd=%s session_tokens=%d session_cost_usd=%.5f",
            model,
            turn_in,
            turn_out,
            cache_read,
            cache_write,
            cost_str,
            self._session_in + self._session_out,
            self._session_cost,
        )

    def _system(self) -> str:
        """System prompt + the injected memory profile (same as the local model)."""
        parts = [SYSTEM_PROMPT]
        if self._memory is not None:
            ctx = self._memory.context()
            if ctx:
                parts.append(ctx)
        return "\n\n".join(parts)

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one turn end-to-end; tool calls run through ``execute`` (the gate).

        Keeps an **append-only** full history (incl. ``tool_use`` / ``tool_result``
        blocks) so the model has a faithful record of what it did, and so the prefix
        stays byte-stable for prompt caching. A cache breakpoint on the last block
        caches tools + system + prior turns; per-turn we log cache hit/miss.
        """
        tools = to_anthropic_tools(self._registry.schemas())
        # Append-only: everything for this turn is added to the live history. On a
        # request failure we roll back to here so a half-built turn can't corrupt it.
        start = len(self._history)
        self._history.append({"role": "user", "content": user_text})

        reply = ""
        turn_in = turn_out = cache_read = cache_write = 0
        for _ in range(_MAX_TOOL_ROUNDS):
            problem = _first_pairing_problem(self._history)
            if problem:  # should never happen — but if it does, say exactly where
                _log.error("history integrity broken before send: %s", problem)
            try:
                resp = self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=self._settings.anthropic_max_tokens,
                    temperature=self._settings.llm_temperature,
                    system=self._system(),
                    messages=with_cache_breakpoint(self._history),
                    tools=tools,
                )
            except Exception as exc:  # cloud rejected/unreachable — stay useful
                _log.warning("cloud request failed: %s", exc)
                del self._history[start:]  # abandon this turn; keep history valid
                return cloud_error_reply(exc)
            usage = _get(resp, "usage")
            turn_in += int(_get(usage, "input_tokens") or 0)
            turn_out += int(_get(usage, "output_tokens") or 0)
            cache_read += int(_get(usage, "cache_read_input_tokens") or 0)
            cache_write += int(_get(usage, "cache_creation_input_tokens") or 0)
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

        self._log_usage(turn_in, turn_out, cache_read, cache_write)
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)
        return reply


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
