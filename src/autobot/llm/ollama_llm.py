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
from autobot.tools.registry import ToolRegistry

_log = get_logger("llm")

SYSTEM_PROMPT = (
    "You are Autobot, a local voice assistant. Your replies are spoken aloud, so "
    "talk like a person in conversation. Rules:\n"
    "- Reply in one to three short, natural sentences.\n"
    "- Never use lists, numbering, bullet points, markdown, or headings.\n"
    "- Never read out URLs, web addresses, or source names — just say the answer.\n"
    "- When tools or web results give you facts, weave them into a friendly "
    "spoken summary rather than reciting them.\n"
    "- Always respond in English, and use the provided tools when they help."
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

    def __init__(self, settings: Settings, registry: ToolRegistry) -> None:
        from ollama import Client

        self._settings = settings
        self._registry = registry
        self._client = Client(host=settings.ollama_host)

    def _chat(self, messages: list[dict[str, Any]]) -> Any:
        """Call Ollama with bounded output; disable 'thinking' for qwen3 models."""
        model = self._settings.llm_model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": self._registry.schemas(),
            "options": {
                "temperature": self._settings.llm_temperature,
                # Bound reply length so spoken answers stay short and fast.
                "num_predict": self._settings.llm_max_tokens,
            },
        }
        # Only qwen3 has a reasoning mode worth disabling; other models reject the
        # ``think`` kwarg or don't need it.
        if "qwen3" in model:
            try:
                return self._client.chat(think=False, **kwargs)
            except TypeError:
                # Older ollama-python without the ``think`` keyword.
                return self._client.chat(**kwargs)
        return self._client.chat(**kwargs)

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user turn end-to-end; see the interface for the contract."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        response = self._chat(messages)
        message = _get(response, "message")
        calls = normalize_tool_calls(message)

        if not calls:
            _log.debug("planned no tool calls model=%s", self._settings.llm_model)
            return message_content(message)

        _log.info("planned tools=%s model=%s", [c.name for c in calls], self._settings.llm_model)
        messages.append(_to_message_dict(message))
        for call in calls:
            # Execution goes through the injected executor (the permission gate),
            # never the registry directly — this is the gate's seam.
            result = execute(call)
            messages.append({"role": "tool", "tool_name": call.name, "content": result.content})

        final = self._chat(messages)
        return message_content(_get(final, "message"))
