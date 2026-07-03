"""The provider-agnostic model seam the :class:`~autobot.agent.harness.AgentHarness` drives.

A :class:`ChatModel` owns its own provider-native conversation history, caching,
trimming, and compaction. The harness only orchestrates the *round loop*, calling
these primitives — so swapping a provider (or adding a new one) never touches the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from autobot.core.types import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """One assistant response: its final text and any tool calls it requested."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class ChatModel(Protocol):
    """A provider adapter exposing turn primitives the harness drives.

    The adapter records the assistant message and tool results in its own native
    history during ``send``/``record_results``; the harness never sees provider
    message shapes. ``handle_discovery`` lets a provider that discovers tools
    *client-side* (e.g. Ollama's ``find_tools``) service such a call inline; a
    provider using server-side search returns ``None``.
    """

    def begin_turn(self, user_text: str) -> None:
        """Start a turn: record the user message and reset per-turn state."""
        ...

    def send(self) -> ChatResponse:
        """Assemble + send the current history, record the assistant reply natively."""
        ...

    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append this round's tool results to the native history, in call order."""
        ...

    def handle_discovery(self, call: ToolCall) -> str | None:
        """Service a client-side tool-discovery call, or ``None`` if not one."""
        ...

    def final_answer_no_tools(self) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        ...

    def finalize_turn(self) -> None:
        """Post-turn housekeeping: compaction, usage reporting, history trim."""
        ...

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot, non-conversational completion (no tools)."""
        ...

    def context_usage(self) -> dict[str, Any] | None:
        """Context-meter payload, or ``None`` before the first turn."""
        ...

    def new_session(self) -> None:
        """Discard conversation history and start fresh."""
        ...

    def set_delivery_mode(self, mode: str) -> None:
        """Set how the next reply is delivered (``"chat"`` = text, else spoken)."""
        ...
