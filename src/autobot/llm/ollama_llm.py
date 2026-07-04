"""Ollama-backed implementation of :class:`~autobot.core.interfaces.LanguageModel`.

The turn does the *full* tool round-trip, which is the whole point of Phase 0:

    user text -> model -> (tool_calls?) -> run tools -> feed results -> final reply

The message-parsing helpers (:func:`normalize_tool_calls`, :func:`message_content`)
are pure functions so they can be unit-tested without a live Ollama server.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autobot.agent.session import Session
    from autobot.core.interfaces import ToolSelector

from autobot.agent.chat_model import ChatResponse
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.memory.store import MemoryStore
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.builtin import FIND_TOOLS
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
    "You are Jack, a local assistant for the user's Mac. You're a warm, friendly "
    "person, not a robotic tool. Principles:\n"
    "- You ACT through your tools. A request phrased as a question ('can you…', "
    "'could you…', 'will you…') is a command to act, not a yes/no question. When "
    "the user asks for something a tool can do, you MUST call that tool to actually "
    "do it — never reply as if it's done without calling the tool, and never claim "
    "an action or result you didn't get back from a tool this turn. Don't say what "
    "you're about to do and don't write a tool's name in your reply; call the tool, "
    "then say only what actually happened. And never tell the user to do it "
    "themselves (click a button, use a menu, or open/check an app or website) — you "
    "do it for them.\n"
    "- You can take several steps in one turn: call a tool, look at its result, then "
    "decide the next action — use one tool's output to inform the next. Only call a "
    "tool when you actually need to act or look something up; otherwise just answer. "
    "Don't repeat a call that already failed. Once you have what you need, give your "
    "final answer.\n"
    "- You work in an ACTIVE folder (your current working directory). Create and "
    "edit files there by default. If the user asks to save something clearly "
    "unrelated to that folder, ask whether to save it there or pick another place.\n"
    "- Pick the tool whose description matches the user's intent. If you're unsure "
    "which the user means, ask one short question instead of guessing.\n"
    "- Acknowledgments and pleasantries ('thanks', 'okay', 'cool', 'never mind', "
    "'got it') are NOT commands: reply in a few words and do not call a tool, unless "
    "they clearly confirm an action you just offered.\n"
    "- To open a website or web service (YouTube, Gmail, a news site, any URL), use "
    "open_website to take them straight there — don't just open a blank browser or "
    "tell them to navigate.\n"
    "- When you know the user's name, use it naturally now and then, and let what "
    "you remember about them shape your replies — without reciting their saved "
    "details back at them.\n"
    "- When the user shares durable information about themselves (their name, what "
    "they like, do, or prefer), quietly save it with set_name/remember so you know "
    "it next time. Don't save passwords, financial, or health details.\n"
    "- For anything current, recent, time-sensitive, or that you're unsure of, use "
    "web_search rather than saying you can't know; then answer in your own words "
    "without reading out URLs or source names.\n"
    "- Don't repeat a previous answer; if asked for more, add new specifics. Don't "
    "list your capabilities or ask 'what next?' unless asked.\n"
    "- Always respond in English."
)

# How the reply is delivered depends on the turn's mode — spoken vs. shown as text.
# Kept separate from SYSTEM_PROMPT so the principles stay shared and only the
# delivery instruction changes per turn (see system_prompt / Session.delivery_mode).
VOICE_DELIVERY = (
    "Your reply will be spoken aloud. Talk like a person and keep it SHORT — one "
    "sentence, two at most — with no lists, numbering, markdown, or headings."
)
CHAT_DELIVERY = (
    "Your reply is shown as text in a chat, not spoken. Be concise and "
    "conversational; you may use light markdown (a short list, `code`, or a link) "
    "when it genuinely aids readability. Don't phrase replies as speech or say "
    "you'll read anything out."
)


def system_prompt(mode: str) -> str:
    """The system prompt with a delivery line matched to the turn's mode.

    Args:
        mode: ``"chat"`` (reply shown as text) or anything else (spoken/voice).
    """
    delivery = CHAT_DELIVERY if mode == "chat" else VOICE_DELIVERY
    return f"{SYSTEM_PROMPT}\n{delivery}"


def active_folder_line() -> str:
    """A one-line 'Active folder: <path>' for the system context, or '' if unknown."""
    from autobot.tools.access import active_policy

    pol = active_policy()
    return f"Active folder: {pol.cwd}" if pol is not None else ""


def meeting_state_line() -> str:
    """A one-line live meeting-recorder status for the system context.

    Empty when meetings aren't enabled. Otherwise it is authoritative about whether a
    recording is in progress *right now* — injected every turn so the model reflects
    reality instead of a stale earlier message. A meeting can be stopped from the
    drawer's Stop button (which bypasses the model), so without this the model would
    keep thinking it is recording and decline to start a new one.
    """
    from autobot.meeting.state import meeting_status_snapshot

    st = meeting_status_snapshot()
    if st is None:
        return ""
    if st.get("active"):
        mins = int(float(st.get("elapsed_s", 0.0) or 0.0) // 60)
        paused = " (currently paused)" if st.get("paused") else ""
        return (
            f"Meeting recorder: a recording IS in progress right now{paused} "
            f"(~{mins} min elapsed). To finish it, use stop_meeting; do not start another."
        )
    return "Meeting recorder: idle — no meeting is being recorded right now."


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
        client: Any | None = None,
        selector: ToolSelector | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._selector = selector
        self._round_query = ""  # current turn's user text; the relevance signal
        self._pinned: set[str] = set()  # tools discovered via find_tools, this turn only
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        if client is not None:  # injected (tests)
            self._client = client
        else:
            from ollama import Client

            self._client = Client(host=settings.ollama_host)
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0  # this turn's generated output (for the "This turn" line)
        self._context_tokens = self._resolve_context()
        _log.info("context window=%d tokens model=%s", self._context_tokens, settings.llm_model)
        # Per-turn buffers shared by the ChatModel primitives (begin_turn/send/…). Valid
        # only during one serialized turn; re-initialized in begin_turn from the session.
        self._messages: list[dict[str, Any]] = []  # this turn's working message list
        self._sent_start = 0  # index in _messages where this turn's tool exchange begins
        self._user_msg: dict[str, Any] = {}  # this turn's user message (persisted at finalize)

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
            kwargs["tools"] = self._tools_for_round()
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

    def _tools_for_round(self) -> list[dict[str, Any]]:
        """Schemas to advertise this round: the selector's subset, or all tools.

        With a selector wired, advertise the relevance-gated subset for this turn's
        message (including any tools pinned by ``find_tools`` so far), plus the
        always-on ``find_tools`` meta-tool so the model can discover what the
        pre-filter missed. Without a selector, every registered tool is advertised —
        the original behavior, kept so existing callers/tests are unaffected (and
        ``find_tools`` is pointless there: nothing is gated, nothing to discover).
        """
        if self._selector is None:
            return self._registry.schemas()
        selected = self._selector.select(self._round_query, pinned=frozenset(self._pinned))
        return [spec.to_schema() for spec in selected] + [FIND_TOOLS.to_schema()]

    def _discover_tools(self, intent: str) -> str:
        """Run the find_tools escape hatch: search, pin matches, summarize for the model.

        Asks the selector for the gated tools best matching ``intent``, pins their
        names so :meth:`_tools_for_round` advertises them for the rest of this turn,
        and returns a short ``name: description`` summary the model can read to pick
        the right tool on its next step. With no selector (legacy path) ``find_tools``
        is never advertised, so this is only reached when ``self._selector`` is set.
        """
        if self._selector is None:  # defensive; find_tools isn't advertised without one
            return "Tool discovery is unavailable."
        names = self._selector.search(intent)
        self._pinned.update(names)
        specs = [self._registry.get(name) for name in names]
        found = [s for s in specs if s is not None]
        _log.info("find_tools intent=%r matched=%s", intent, [s.name for s in found])
        if not found:
            return f"No tools found for: {intent}. Tell the user you can't do that."
        lines = [f"- {s.name}: {s.description}" for s in found]
        return "Found these tools (now available to call):\n" + "\n".join(lines)

    def _assemble(self, session: Session, user_msg: dict[str, Any]) -> list[dict[str, Any]]:
        """System prompt + running summary (if any) + recent turns + the new message."""
        mode = session.delivery_mode
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(mode)}]
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

    def begin_turn(self, session: Session, user_text: str) -> None:
        """Start a turn: reset per-turn state, compact pre-flight, assemble messages."""
        self._user_msg = {"role": "user", "content": user_text}
        self._round_query = user_text  # relevance signal for tool selection this turn
        self._pinned = set()  # find_tools discoveries are per-turn; never leak across turns
        # Proactive: compact BEFORE sending if this prompt would cross the budget.
        estimated = estimate_tokens(self._assemble(session, self._user_msg))
        self._compact_if_needed(session, estimated, source="preflight")
        self._messages = self._assemble(session, self._user_msg)
        self._sent_start = len(self._messages)

    def send(self, session: Session) -> ChatResponse:
        """Call the model once, record the assistant message, return text + tool calls."""
        response = self._chat(self._messages)
        message = _get(response, "message")
        calls = normalize_tool_calls(message)
        self._messages.append(_to_message_dict(message))  # record assistant turn faithfully
        if not calls:
            _log.debug("planned no tool calls model=%s", self._settings.llm_model)
        return ChatResponse(text=message_content(message), tool_calls=calls)

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        """Service a ``find_tools`` call inline; ``None`` for any normal tool call."""
        if call.name == FIND_TOOLS.name and self._selector is not None:
            return self._discover_tools(call.arguments.get("intent", ""))
        return None

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append this round's tool results to the working messages, in call order."""
        for call, result in results:
            self._messages.append(
                {"role": "tool", "tool_name": call.name, "content": result.content}
            )

    def finalize_turn(self, session: Session) -> list[dict[str, Any]]:
        """Persist this turn append-only, then post-turn compact; return new messages."""
        new = [self._user_msg, *self._messages[self._sent_start :]]
        session.history.extend(new)
        session.history = trim_history(session.history, _HARD_MAX_MESSAGES)
        self._compact_if_needed(session, self._last_prompt_tokens, source="post-turn")
        self._report_usage(session)
        return new

    def final_answer_no_tools(self, session: Session) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        _log.info("tool-round cap reached; forcing a final answer without tools")
        try:
            response = self._chat(self._messages, with_tools=False)
        except Exception:
            _log.exception("forced final answer failed")
            return "Sorry, that took too many steps."
        message = _get(response, "message")
        self._messages.append(_to_message_dict(message))
        return message_content(message) or "Sorry, that took too many steps."

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion via Ollama chat (no tools advertised).

        A single non-conversational call — no history, no tools, no executor. Used
        by the meeting summarizer and similar batch tasks that need a plain LLM
        completion rather than a full interactive turn.

        Args:
            prompt: The full prompt to send.
            temperature: Sampling temperature; 0.0 for deterministic output.

        Returns:
            The model's reply text, stripped of leading/trailing whitespace.
        """
        model = self._settings.llm_model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": temperature},
        }
        if "qwen3" in model:
            try:
                response = self._client.chat(think=False, **kwargs)
            except TypeError:
                response = self._client.chat(**kwargs)
        else:
            response = self._client.chat(**kwargs)
        return str(message_content(_get(response, "message"))).strip()

    def _report_usage(self, session: Session) -> None:
        """Write the context-meter payload into the session, and log/echo it.

        Local has no prompt-cache billing, so cache_read/write are None (the card
        hides those rows).
        """
        ctx = self._context_tokens
        pct = round(100 * self._last_prompt_tokens / ctx) if ctx else 0
        _log.info("turn prompt_tokens=%d ctx=%d pct=%d", self._last_prompt_tokens, ctx, pct)
        self._transcript.note(f"{self._last_prompt_tokens}/{ctx} tokens ({pct}% of context)")
        if self._settings.show_debug:
            print(f"[ctx] {self._last_prompt_tokens}/{ctx} tokens ({pct}%)")
        if ctx and self._last_prompt_tokens > ctx:
            _log.warning("context limit exceeded prompt=%d ctx=%d", self._last_prompt_tokens, ctx)
        if not self._last_prompt_tokens or not ctx:
            session.last_usage = None
            return
        session.last_usage = {
            "used": self._last_prompt_tokens,
            "window": ctx,
            "cache_read": None,
            "cache_write": None,
            # Local has no prompt cache, so the whole prompt is processed each turn:
            # "in" for the turn equals the prompt size.
            "turn_in": self._last_prompt_tokens,
            "turn_out": self._last_eval_tokens,
            "model": self._settings.llm_model,
        }

    def _compact_if_needed(self, session: Session, prompt_tokens: int, *, source: str) -> None:
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
        kept = trim_history(session.history, keep) if keep > 0 else []
        older = session.history[: len(session.history) - len(kept)]
        if not older:
            return
        session.summary = self._summarize(session.summary, older)
        session.history = kept
        pct = round(100 * prompt_tokens / ctx) if ctx else 0
        _log.info(
            "compacted source=%s prompt_tokens=%d ctx=%d pct=%d summarized=%d kept=%d",
            source,
            prompt_tokens,
            ctx,
            pct,
            len(older),
            len(session.history),
        )
        self._transcript.note(
            f"compaction ({source}) at {pct}% — summarized {len(older)} older "
            f"messages, kept {len(session.history)}"
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
