"""OpenAI-compatible ``chat.completions`` adapter implementing :class:`ChatModel`.

Speaks the OpenAI Chat Completions dialect, which OpenAI, OpenRouter, Groq,
Together, DeepSeek, Mistral, local vLLM/LM Studio, Gemini's OpenAI-compat
endpoint, and Ollama's ``/v1`` all accept — so "any LLM via API key" is one
adapter parameterized by base URL + model + key. Structure mirrors
:class:`~autobot.llm.ollama_llm.OllamaLanguageModel` (same message shape); the
differences are the SDK call, response parsing, and ``tool_call_id`` pairing.

Using a cloud endpoint sends the conversation + tool schemas/results to that
provider — a disclosed, opt-in exception (like the Anthropic path). The key is
read from the keyring; audio never leaves the device.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from autobot.agent.chat_model import ChatResponse, OnEvent
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.ollama_llm import (
    active_folder_line,
    estimate_tokens,
    meeting_state_line,
    needs_compaction,
    render_messages,
    system_prompt,
    trim_history,
)
from autobot.logging_setup import get_logger
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.builtin import FIND_TOOLS
from autobot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from autobot.agent.session import Session
    from autobot.core.interfaces import ToolSelector
    from autobot.memory.store import MemoryStore

_log = get_logger("provider")

_DEFAULT_CONTEXT_TOKENS = 8192
# Mirrors Ollama's backstop (`llm/ollama_llm.py`) on purpose — kept as a local constant
# rather than a shared import so the two providers can diverge independently (Anthropic
# intentionally uses a different backstop). Not drift; don't "fix" by unifying.
_HARD_MAX_MESSAGES = 100
_SUMMARIZE_INSTRUCTION = (
    "Summarize the conversation so far in a few sentences. Preserve the user's goals, "
    "key facts, decisions, and any tool/web results. Be concise; this replaces older turns."
)


def _assembled_completion(content: str, frags: dict[int, dict[str, str]]) -> Any:
    """Build a synthetic completion from streamed text + assembled tool-call fragments.

    Shaped so :meth:`OpenAICompatibleModel._parse` — which reads a completion via
    ``getattr`` — sees the same attribute surface (`.choices[0].message.content`,
    `.tool_calls[i].id`, `.function.name`, `.function.arguments`) as a real,
    non-streamed ``ChatCompletion``. ``frags`` maps each tool call's stream ``index``
    to its assembled ``{"id", "name", "args"}`` (``args`` is the concatenation of every
    fragment's partial JSON string for that index).
    """
    tool_calls = [
        SimpleNamespace(
            id=frag["id"] or None,
            function=SimpleNamespace(name=frag["name"] or None, arguments=frag["args"]),
        )
        for _, frag in sorted(frags.items())
    ]
    message = SimpleNamespace(content=content or None, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class OpenAICompatibleModel:
    """Runs turns against any OpenAI-compatible chat.completions endpoint."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
        client: Any | None = None,
        selector: ToolSelector | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._selector = selector
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        self._round_query = ""
        self._pinned: set[str] = set()
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0
        self._context_tokens = settings.context_tokens or _DEFAULT_CONTEXT_TOKENS
        # Per-turn buffers shared by the ChatModel primitives. Valid only during one
        # serialized turn; re-initialized in begin_turn from the passed session.
        self._messages: list[dict[str, Any]] = []
        self._sent_start = 0
        self._user_msg: dict[str, Any] = {}
        self._last_tool_calls: list[dict[str, Any]] = []  # assistant tool_calls to pair results
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            from autobot.secrets import get_secret

            key = get_secret("openai_api_key") or "not-needed"  # local servers ignore the key
            self._client = OpenAI(base_url=settings.openai_base_url or None, api_key=key)
        _log.info(
            "openai-compatible ready base_url=%s model=%s",
            settings.openai_base_url or "(default)",
            settings.llm_model,
        )

    # --- prompt assembly (mirrors Ollama) ---
    def _assemble(self, session: Session, user_msg: dict[str, Any]) -> list[dict[str, Any]]:
        coder = self._settings.profile == "coder"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(session.delivery_mode, coder=coder)}
        ]
        if self._memory is not None:
            profile = self._memory.context()
            if profile:
                messages.append({"role": "system", "content": profile})
        folder = active_folder_line()
        if folder:
            messages.append({"role": "system", "content": folder})
        meeting = meeting_state_line()
        if meeting:
            messages.append({"role": "system", "content": meeting})
        if session.summary:
            messages.append(
                {"role": "system", "content": f"Summary of earlier conversation: {session.summary}"}
            )
        messages += session.history
        messages.append(user_msg)
        return messages

    def _tools_for_round(self) -> list[dict[str, Any]]:
        if self._selector is None:
            return self._registry.schemas()
        selected = self._selector.select(self._round_query, pinned=frozenset(self._pinned))
        return [spec.to_schema() for spec in selected] + [FIND_TOOLS.to_schema()]

    def _discover_tools(self, intent: str) -> str:
        if self._selector is None:
            return "Tool discovery is unavailable."
        names = self._selector.search(intent)
        self._pinned.update(names)
        specs = [self._registry.get(n) for n in names]
        found = [s for s in specs if s is not None]
        if not found:
            return f"No tools found for: {intent}. Tell the user you can't do that."
        return "Found these tools (now available to call):\n" + "\n".join(
            f"- {s.name}: {s.description}" for s in found
        )

    # --- one model call ---
    def _create(
        self,
        messages: list[dict[str, Any]],
        *,
        with_tools: bool = True,
        on_event: OnEvent | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
        }
        if with_tools:
            tools = self._tools_for_round()
            if tools:
                kwargs["tools"] = tools
        if on_event is not None:
            kwargs["stream"] = True
            # Best-effort: ask compliant backends to include a final usage chunk on the
            # stream (the loop below already reads `chunk.usage` when present); backends
            # that reject the option simply ignore it.
            kwargs["stream_options"] = {"include_usage": True}
            content = ""
            frags: dict[int, dict[str, str]] = {}  # stream index -> {id, name, args}
            prompt_tok = eval_tok = 0
            for chunk in self._client.chat.completions.create(**kwargs):
                choice = chunk.choices[0] if chunk.choices else None
                delta = getattr(choice, "delta", None)
                piece = getattr(delta, "content", None) or ""
                if piece:
                    on_event({"type": "token", "text": piece})
                    content += piece
                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", 0)
                    slot = frags.setdefault(index, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    frag_name = getattr(fn, "name", None)
                    if frag_name:
                        slot["name"] = frag_name
                    frag_args = getattr(fn, "arguments", None)
                    if frag_args:
                        slot["args"] += frag_args
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    prompt_tok = int(getattr(usage, "prompt_tokens", 0) or prompt_tok)
                    eval_tok = int(getattr(usage, "completion_tokens", 0) or eval_tok)
            self._last_prompt_tokens = prompt_tok
            self._last_eval_tokens = eval_tok
            return _assembled_completion(content, frags)
        resp = self._client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        self._last_prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        self._last_eval_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return resp

    @staticmethod
    def _parse(resp: Any) -> tuple[str, list[ToolCall], dict[str, Any]]:
        """Return (text, tool_calls, assistant_message_dict) from a completion."""
        choice = resp.choices[0]
        msg = choice.message
        text = (getattr(msg, "content", None) or "").strip()
        raw_calls = getattr(msg, "tool_calls", None) or []
        calls: list[ToolCall] = []
        recorded_calls: list[dict[str, Any]] = []
        for tc in raw_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            args_str = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(args_str)
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            # Fall back to a position-unique id (not just ``name``) so two id-less calls to
            # the same tool in one round don't collide — OpenAI requires unique
            # tool_call_ids, and ``record_results`` pairs by this same position anyway.
            call_id = getattr(tc, "id", None) or f"{name}_{len(recorded_calls)}"
            calls.append(ToolCall(name=name, arguments=args))
            recorded_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                }
            )
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if recorded_calls:
            assistant["tool_calls"] = recorded_calls
        return text, calls, assistant

    # --- ChatModel primitives ---
    def begin_turn(self, session: Session, user_text: str) -> None:
        """Start a turn: reset per-turn state, compact pre-flight, assemble messages."""
        self._user_msg = {"role": "user", "content": user_text}
        self._round_query = user_text
        self._pinned = set()
        estimated = estimate_tokens(self._assemble(session, self._user_msg))
        if needs_compaction(estimated, self._context_tokens, self._settings.compact_at):
            self._compact(session)
        self._messages = self._assemble(session, self._user_msg)
        self._sent_start = len(self._messages)
        self._last_tool_calls = []

    def send(self, session: Session, on_event: OnEvent | None = None) -> ChatResponse:
        """Call the model once, record the assistant message, return text + tool calls.

        When ``on_event`` is given, streams ``{"type": "token", ...}`` events as text
        arrives and assembles any fragmented tool calls; ``None`` keeps the original
        single-shot blocking call.
        """
        resp = self._create(self._messages, on_event=on_event)
        text, calls, assistant = self._parse(resp)
        self._messages.append(assistant)
        self._last_tool_calls = assistant.get("tool_calls", [])
        return ChatResponse(text=text, tool_calls=calls)

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        """Service a ``find_tools`` call inline; ``None`` for any normal tool call."""
        if call.name == FIND_TOOLS.name and self._selector is not None:
            return self._discover_tools(call.arguments.get("intent", ""))
        return None

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append tool results, paired to the last assistant tool_calls' ids by order."""
        ids = [tc.get("id") for tc in self._last_tool_calls]
        for i, (call, result) in enumerate(results):
            tool_call_id = ids[i] if i < len(ids) else call.name
            self._messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": result.content}
            )

    def final_answer_no_tools(self, session: Session) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        _log.info("tool-round cap reached; forcing a final answer without tools")
        try:
            resp = self._create(self._messages, with_tools=False)
        except Exception:
            _log.exception("forced final answer failed")
            return "I hit my step limit; partial changes are saved."
        text, _calls, assistant = self._parse(resp)
        self._messages.append(assistant)
        return text or "I hit my step limit; partial changes are saved."

    def finalize_turn(self, session: Session) -> list[dict[str, Any]]:
        """Persist this turn append-only, then post-turn compact; return new messages."""
        new = [self._user_msg, *self._messages[self._sent_start :]]
        session.history.extend(new)
        session.history = trim_history(session.history, _HARD_MAX_MESSAGES)
        if needs_compaction(
            self._last_prompt_tokens, self._context_tokens, self._settings.compact_at
        ):
            self._compact(session)
        pct = (
            round(100 * self._last_prompt_tokens / self._context_tokens)
            if self._context_tokens
            else 0
        )
        _log.info(
            "turn prompt_tokens=%d ctx=%d pct=%d",
            self._last_prompt_tokens,
            self._context_tokens,
            pct,
        )
        if not self._last_prompt_tokens or not self._context_tokens:
            session.last_usage = None
            return new
        session.last_usage = {
            "used": self._last_prompt_tokens,
            "window": self._context_tokens,
            "cache_read": None,
            "cache_write": None,
            "turn_in": self._last_prompt_tokens,
            "turn_out": self._last_eval_tokens,
            "model": self._settings.llm_model,
        }
        return new

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion (no tools, no history)."""
        resp = self._client.chat.completions.create(
            model=self._settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return (getattr(resp.choices[0].message, "content", None) or "").strip()

    # --- compaction (mirrors Ollama) ---
    def _compact(self, session: Session) -> None:
        keep = self._settings.keep_recent_messages
        kept = trim_history(session.history, keep) if keep > 0 else []
        older = session.history[: len(session.history) - len(kept)]
        if not older:
            return
        body = (
            f"Previous summary: {session.summary}\n\n" if session.summary else ""
        ) + render_messages(older)
        try:
            resp = self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=[
                    {"role": "system", "content": _SUMMARIZE_INSTRUCTION},
                    {"role": "user", "content": body},
                ],
                temperature=0.0,
            )
            session.summary = (
                getattr(resp.choices[0].message, "content", None) or ""
            ).strip() or session.summary
        except Exception:
            _log.warning("summarization failed; keeping previous summary")
            return
        session.history = kept
        _log.info("compacted summarized=%d kept=%d", len(older), len(kept))
