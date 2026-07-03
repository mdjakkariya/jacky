"""The provider-agnostic model seam the :class:`~autobot.agent.harness.AgentHarness` drives.

A :class:`ChatModel` is stateless across turns: all conversation state (history,
summary, delivery mode, usage) lives on the :class:`~autobot.agent.session.Session`
the harness passes into every primitive. The harness owns the round loop and the
session; the adapter only reads/writes the session it's given — so swapping a
provider (or adding a new one) never touches the loop, and the harness can persist
or resume a conversation without knowing provider message shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from autobot.agent.session import Session
    from autobot.core.types import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """One assistant response: its final text and any tool calls it requested."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class ChatModel(Protocol):
    """A provider adapter exposing turn primitives the harness drives.

    Every primitive takes the :class:`~autobot.agent.session.Session` for the
    current conversation; the adapter records the assistant message and tool
    results in ``session.history`` during ``send``/``record_results`` — the
    harness never sees provider message shapes. ``handle_discovery`` lets a
    provider that discovers tools *client-side* (e.g. Ollama's ``find_tools``)
    service such a call inline; a provider using server-side search returns
    ``None``.
    """

    def begin_turn(self, session: Session, user_text: str) -> None:
        """Start a turn: record the user message and reset per-turn state."""
        ...

    def send(self, session: Session) -> ChatResponse:
        """Assemble + send the current history, record the assistant reply natively."""
        ...

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append this round's tool results to the native history, in call order."""
        ...

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        """Service a client-side tool-discovery call, or ``None`` if not one."""
        ...

    def final_answer_no_tools(self, session: Session) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        ...

    def finalize_turn(self, session: Session) -> None:
        """Post-turn housekeeping: compaction, usage reporting, history trim."""
        ...

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot, non-conversational completion (no tools)."""
        ...
