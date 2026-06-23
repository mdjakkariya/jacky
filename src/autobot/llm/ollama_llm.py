"""Ollama-backed implementation of :class:`~autobot.core.interfaces.LanguageModel`.

The turn does the *full* tool round-trip, which is the whole point of Phase 0:

    user text -> model -> (tool_calls?) -> run tools -> feed results -> final reply

The message-parsing helpers (:func:`normalize_tool_calls`, :func:`message_content`)
are pure functions so they can be unit-tested without a live Ollama server.
"""

from __future__ import annotations

import json
from typing import Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolExecutor
from autobot.logging_setup import get_logger
from autobot.memory.store import MemoryStore
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.registry import ToolRegistry

_log = get_logger("llm")

_DEFAULT_CONTEXT_TOKENS = 4096  # fallback when the model's window can't be detected
_HARD_MAX_MESSAGES = 100  # safety backstop so history can't grow unbounded
_CHARS_PER_TOKEN = 4  # rough English token estimate for pre-flight compaction

_SUMMARIZE_INSTRUCTION = (
    "Summarize the conversation so far in a few sentences. Preserve the user's "
    "goals, key facts they shared, decisions made, and any tool/web results. Be "
    "concise; this summary replaces the older turns as memory."
)

# General, stable principles only. Per-tool guidance (when to use a tool, what
# words map to it) belongs in that tool's `description`, not here — so adding a
# tool never means editing this prompt. Keep this list short and principled.
SYSTEM_PROMPT = (
    "You are Autobot, a local voice assistant. Replies are spoken aloud, so talk "
    "like a person and keep it SHORT: answer what was asked in one sentence, two "
    "at most. Principles:\n"
    "- You ACT through your tools. A request phrased as a question ('can you…', "
    "'could you…', 'will you…') is a command to act, not a yes/no question. When "
    "the user asks for something a tool can do, you MUST call that tool to actually "
    "do it — never reply as if it's done without calling the tool, and never claim "
    "an action or result you didn't get back from a tool this turn. Don't say what "
    "you're about to do and don't write a tool's name in your reply; call the tool, "
    "then say only what actually happened. And never tell the user to do it "
    "themselves (click a button, use a menu, or open/check an app or website) — you "
    "do it for them.\n"
    "- Pick the tool whose description matches the user's intent. If you're unsure "
    "which the user means, ask one short question instead of guessing.\n"
    "- Acknowledgments and pleasantries ('thanks', 'okay', 'cool', 'never mind', "
    "'got it') are NOT commands: reply in a few words and do not call a tool, unless "
    "they clearly confirm an action you just offered.\n"
    "- To open a website or web service (YouTube, Gmail, a news site, any URL), use "
    "open_website to take them straight there — don't just open a blank browser or "
    "tell them to navigate.\n"
    "- You're a warm, friendly companion, not a robotic tool. When you know the "
    "user's name, use it naturally now and then, and let what you remember about "
    "them shape your replies — without reciting their saved details back at them.\n"
    "- When the user shares durable information about themselves (their name, what "
    "they like, do, or prefer), quietly save it with set_name/remember so you know "
    "it next time. Don't save passwords, financial, or health details.\n"
    "- For anything current, recent, time-sensitive, or that you're unsure of, use "
    "web_search rather than saying you can't know; then answer in your own words "
    "without reading out URLs or source names.\n"
    "- Don't repeat a previous answer; if asked for more, add new specifics. Don't "
    "list your capabilities or ask 'what next?' unless asked.\n"
    "- No lists, numbering, markdown, or headings. Always respond in English."
)


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a dict, or the attribute ``key`` from an object.

    Ollama responses may be plain dicts or pydantic models depending on the
    client version; this normalizes both.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def message_content(message: Any) -> str:
    """Return the stripped text content of an Ollama chat message."""
    return (_get(message, "content") or "").strip()


def normalize_tool_calls(message: Any) -> list[ToolCall]:
    """Extract tool calls from a chat message into typed :class:`ToolCall`s.

    Handles dict- and object-shaped messages and arguments delivered either as a
    dict or as a JSON string. Unparseable arguments degrade to ``{}`` rather than
    raising.

    Args:
        message: The ``message`` field of an Ollama chat response.

    Returns:
        A list of :class:`~autobot.core.types.ToolCall`, possibly empty.
    """
    raw = _get(message, "tool_calls") or []
    calls: list[ToolCall] = []
    for tc in raw:
        fn = _get(tc, "function") or {}
        name = _get(fn, "name")
        if not name:
            continue
        args = _get(fn, "arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCall(name=name, arguments=args))
    return calls


def trim_history(history: list[dict[str, Any]], max_messages: int) -> list[dict[str, Any]]:
    """Keep the most recent ``max_messages`` entries (0 disables history).

    Never starts mid tool-exchange: drops leading ``assistant``/``tool`` messages
    until a real ``user`` turn, so a ``tool`` result can't appear without the
    assistant ``tool_calls`` that produced it.
    """
    if max_messages <= 0:
        return []
    trimmed = history[-max_messages:]
    while trimmed and trimmed[0].get("role") != "user":
        trimmed.pop(0)
    return trimmed


def pick_context_length(model_info: dict[str, Any] | None, default: int) -> int:
    """Find the model's context window from Ollama's model_info, else ``default``.

    The key is architecture-specific (e.g. ``qwen2.context_length``), so we scan
    for any ``*context_length`` entry.
    """
    if model_info:
        for key, value in model_info.items():
            if key.endswith("context_length") and isinstance(value, int) and value > 0:
                return value
    return default


def needs_compaction(prompt_tokens: int, context_tokens: int, threshold: float) -> bool:
    """Whether the prompt uses enough of the window to warrant summarizing."""
    return context_tokens > 0 and prompt_tokens >= int(threshold * context_tokens)


def estimate_tokens(messages: list[dict[str, Any]], chars_per_token: int = _CHARS_PER_TOKEN) -> int:
    """Rough token estimate from message lengths (~4 chars/token for English).

    Used to decide compaction *before* a request, so a single large incoming
    message can't push the actual prompt past the context limit.
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // max(1, chars_per_token)


def render_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten messages to plain text for summarization input."""
    return "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in messages)


def _to_message_dict(message: Any) -> dict[str, Any]:
    """Best-effort convert a (possibly pydantic) message back to a dict."""
    for attr in ("model_dump", "dict"):
        fn = getattr(message, attr, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:  # fall through to the manual shape
                pass
    if isinstance(message, dict):
        return message
    return {"role": "assistant", "content": message_content(message)}


class OllamaLanguageModel:
    """Runs user turns against a local Ollama model with tool calling."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        from ollama import Client

        self._settings = settings
        self._registry = registry
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        self._client = Client(host=settings.ollama_host)
        # Conversational memory: recent {user, assistant} turns kept verbatim, plus
        # a running summary of older turns once the context fills up.
        self._history: list[dict[str, Any]] = []
        self._summary = ""
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0  # this turn's generated output (for the "This turn" line)
        self._context_tokens = self._resolve_context()
        _log.info("context window=%d tokens model=%s", self._context_tokens, settings.llm_model)

    def _resolve_context(self) -> int:
        """Detect the model's context window (or use the configured override)."""
        if self._settings.context_tokens > 0:
            return self._settings.context_tokens
        try:
            info = self._client.show(self._settings.llm_model)
            model_info = _get(info, "modelinfo") or _get(info, "model_info") or {}
            return pick_context_length(model_info, _DEFAULT_CONTEXT_TOKENS)
        except Exception:
            _log.warning("could not detect context length; using %d", _DEFAULT_CONTEXT_TOKENS)
            return _DEFAULT_CONTEXT_TOKENS

    def _chat(self, messages: list[dict[str, Any]], *, with_tools: bool = True) -> Any:
        """Call Ollama with the full window + bounded output; track prompt tokens."""
        model = self._settings.llm_model
        think_on = "qwen3" in model and self._settings.llm_think
        # Reasoning tokens count against num_predict but go to `thinking`, not the
        # spoken reply — so give headroom when it's on, or the answer can be starved.
        predict = (
            max(self._settings.llm_max_tokens, 1024)
            if think_on
            else (self._settings.llm_max_tokens)
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "options": {
                "temperature": self._settings.llm_temperature,
                "num_predict": predict,
                # Use the real window, not Ollama's small default.
                "num_ctx": self._context_tokens,
            },
        }
        if with_tools:
            kwargs["tools"] = self._registry.schemas()
        # Only qwen3 supports the reasoning toggle; other models reject the kwarg.
        if "qwen3" in model:
            try:
                response = self._client.chat(think=think_on, **kwargs)
            except TypeError:
                response = self._client.chat(**kwargs)
        else:
            response = self._client.chat(**kwargs)
        self._last_prompt_tokens = int(_get(response, "prompt_eval_count") or 0)
        self._last_eval_tokens = int(_get(response, "eval_count") or 0)
        return response

    def _assemble(self, user_msg: dict[str, Any]) -> list[dict[str, Any]]:
        """System prompt + running summary (if any) + recent turns + the new message."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if self._memory is not None:
            profile = self._memory.context()
            if profile:
                messages.append({"role": "system", "content": profile})
        if self._summary:
            messages.append(
                {"role": "system", "content": f"Summary of earlier conversation: {self._summary}"}
            )
        messages += self._history
        messages.append(user_msg)
        return messages

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user turn end-to-end; see the interface for the contract."""
        user_msg = {"role": "user", "content": user_text}

        # Proactive: if *this* prompt (including a possibly-large new message) would
        # cross the budget, compact BEFORE sending so we never overflow mid-turn.
        estimated = estimate_tokens(self._assemble(user_msg))
        self._compact_if_needed(estimated, source="preflight")

        messages = self._assemble(user_msg)
        response = self._chat(messages)
        message = _get(response, "message")
        calls = normalize_tool_calls(message)

        # Persist the turn *append-only* and faithfully — including the assistant's
        # tool_calls and each tool result — so the next turn has a real record of
        # what was done ("close it" can resolve the site it just opened), not only
        # the final text. A stable, ordered history also lets Ollama reuse its KV
        # cache for the unchanged prefix.
        turn: list[dict[str, Any]] = [user_msg]
        if not calls:
            _log.debug("planned no tool calls model=%s", self._settings.llm_model)
            reply = message_content(message)
            turn.append({"role": "assistant", "content": reply})
        else:
            _log.info(
                "planned tools=%s model=%s", [c.name for c in calls], self._settings.llm_model
            )
            assistant_msg = _to_message_dict(message)
            messages.append(assistant_msg)
            turn.append(assistant_msg)
            for call in calls:
                # Execution goes through the injected executor (the permission gate),
                # never the registry directly — this is the gate's seam.
                result = execute(call)
                tool_msg = {"role": "tool", "tool_name": call.name, "content": result.content}
                messages.append(tool_msg)
                turn.append(tool_msg)
            final = self._chat(messages)
            reply = message_content(_get(final, "message"))
            turn.append({"role": "assistant", "content": reply})

        # Reactive safety net using the real token count; trim at a clean boundary.
        self._history.extend(turn)
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)  # hard backstop
        self._compact_if_needed(self._last_prompt_tokens, source="post-turn")
        self._report_usage()
        return reply

    def context_usage(self) -> dict[str, Any] | None:
        """Context-meter payload, or None pre-turn. Local has no prompt-cache billing,
        so cache_read/write are None (the card hides those rows)."""
        if not self._last_prompt_tokens or not self._context_tokens:
            return None
        return {
            "used": self._last_prompt_tokens,
            "window": self._context_tokens,
            "cache_read": None,
            "cache_write": None,
            # Local has no prompt cache, so the whole prompt is processed each turn:
            # "in" for the turn equals the prompt size.
            "turn_in": self._last_prompt_tokens,
            "turn_out": self._last_eval_tokens,
            "model": self._settings.llm_model,
        }

    def new_session(self) -> None:
        """Discard all conversation history and start a fresh session.

        Clears the running summary and per-turn token counts so the context meter
        resets. The resolved context window and client are untouched — only the
        conversation is wiped. Drives the chat's "New chat" action.
        """
        self._history = []
        self._summary = ""
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0
        _log.info("session reset (new chat)")

    def _report_usage(self) -> None:
        """Log/echo this turn's token usage for debugging."""
        ctx = self._context_tokens
        pct = round(100 * self._last_prompt_tokens / ctx) if ctx else 0
        _log.info("turn prompt_tokens=%d ctx=%d pct=%d", self._last_prompt_tokens, ctx, pct)
        self._transcript.note(f"{self._last_prompt_tokens}/{ctx} tokens ({pct}% of context)")
        if self._settings.show_debug:
            print(f"[ctx] {self._last_prompt_tokens}/{ctx} tokens ({pct}%)")
        if ctx and self._last_prompt_tokens > ctx:
            _log.warning("context limit exceeded prompt=%d ctx=%d", self._last_prompt_tokens, ctx)

    def _compact_if_needed(self, prompt_tokens: int, *, source: str) -> None:
        """Summarize older turns if ``prompt_tokens`` crosses the compaction threshold.

        Called both pre-flight (estimated tokens) and post-turn (measured tokens),
        so a sudden large message can't slip a turn past the context limit.
        """
        ctx = self._context_tokens
        if not needs_compaction(prompt_tokens, ctx, self._settings.compact_at):
            return
        keep = self._settings.keep_recent_messages
        # Keep a *clean* recent tail (never starting mid tool-exchange); summarize
        # everything before it. Using trim_history keeps the boundary valid.
        kept = trim_history(self._history, keep) if keep > 0 else []
        older = self._history[: len(self._history) - len(kept)]
        if not older:
            return
        self._summary = self._summarize(self._summary, older)
        self._history = kept
        pct = round(100 * prompt_tokens / ctx) if ctx else 0
        _log.info(
            "compacted source=%s prompt_tokens=%d ctx=%d pct=%d summarized=%d kept=%d",
            source,
            prompt_tokens,
            ctx,
            pct,
            len(older),
            len(self._history),
        )
        self._transcript.note(
            f"compaction ({source}) at {pct}% — summarized {len(older)} older "
            f"messages, kept {len(self._history)}"
        )
        if self._settings.show_debug:
            print(f"[ctx] compaction ({source}) at ~{pct}% — summarized older turns")

    def _summarize(self, previous: str, messages: list[dict[str, Any]]) -> str:
        """Fold older messages (and any previous summary) into a concise summary."""
        body = (f"Previous summary: {previous}\n\n" if previous else "") + render_messages(messages)
        try:
            response = self._chat(
                [
                    {"role": "system", "content": _SUMMARIZE_INSTRUCTION},
                    {"role": "user", "content": body},
                ],
                with_tools=False,
            )
            return message_content(_get(response, "message")) or previous
        except Exception:
            _log.exception("summarization failed; keeping previous summary")
            return previous
