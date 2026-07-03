"""The conversation state the AgentHarness owns and threads through a ChatModel.

A :class:`Session` holds everything that persists across turns — the
provider-native message history, the running summary, the delivery mode, and
usage/cost totals — so the provider adapters stay stateless per turn. History is
stored in whatever shape the session's provider uses (dict messages for
OpenAI/Ollama, content-block messages for Anthropic); a session is therefore
tied to its provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnUsage:
    """Running token/cost totals for a session (surfaced in the context meter)."""

    in_tokens: int = 0
    out_tokens: int = 0
    usd: float = 0.0
    priced: bool = False  # False = no list price known for this model (hide the $ row)


@dataclass(slots=True)
class Session:
    """One conversation: identity, working dir, model, and accumulated state."""

    id: str
    cwd: str
    model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    delivery_mode: str = "voice"  # "chat" (text) or else spoken
    last_usage: dict[str, Any] | None = None  # provider-shaped context-meter payload
    cost: TurnUsage = field(default_factory=TurnUsage)

    def reset(self) -> None:
        """Clear conversation + usage for a "new chat" (keeps id/cwd/model)."""
        self.history = []
        self.summary = ""
        self.last_usage = None
        self.cost = TurnUsage()
