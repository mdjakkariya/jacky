"""Memory tools — how Jack learns and recalls who he's talking to.

The model calls these during a normal turn (so learning happens inline, with no
extra round-trip): ``set_name`` when the user gives their name, ``remember`` for
a durable preference/fact, ``forget`` to remove something. All are ``WRITE``
(audited, run without a prompt) — they only touch the local profile DB.

The actual recall is automatic: :meth:`MemoryStore.context` is injected into the
prompt each turn, so Jack doesn't need a tool to *read* memory.
"""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.memory.store import MemoryStore
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("memory")


class MemoryTools:
    """Write side of the user profile, exposed as gated tools."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def set_name(self, name: str) -> str:
        """Save the user's name."""
        self._store.set_name(name)
        _log.info("learned name")
        return f"I'll remember your name, {name.strip()}."

    def remember(self, fact: str) -> str:
        """Save a durable fact/preference about the user."""
        added = self._store.add_fact(fact)
        _log.info("remember added=%s", added)
        return "Got it — I'll remember that." if added else "I already had that noted."

    def forget(self, topic: str) -> str:
        """Forget remembered facts matching a topic."""
        removed = self._store.forget(topic)
        _log.info("forget removed=%d", removed)
        if removed:
            return "Done, I've forgotten that."
        return "I didn't have anything noted about that."

    def specs(self) -> list[ToolSpec]:
        """Return the memory tool specs (all WRITE — local profile only)."""
        return [
            ToolSpec(
                name="set_name",
                description=(
                    "Save the user's name when they tell you it (e.g. 'my name is "
                    "Sam', \"I'm Sam\", 'call me Sam')."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "The user's name."}},
                    "required": ["name"],
                },
                handler=self.set_name,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="remember",
                description=(
                    "Save a durable fact or preference the user shares about "
                    "themselves (e.g. 'I love jazz', 'I work at BrowserStack', 'I "
                    "prefer short answers') so you recall it in future sessions. Do "
                    "NOT save passwords, financial, or health details."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "A short fact about the user, e.g. 'likes jazz'.",
                        }
                    },
                    "required": ["fact"],
                },
                handler=self.remember,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="forget",
                description=(
                    "Remove something you remembered about the user when they ask "
                    "you to forget it (e.g. 'forget that I like jazz')."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "What to forget, e.g. 'jazz'."}
                    },
                    "required": ["topic"],
                },
                handler=self.forget,
                risk=Risk.WRITE,
            ),
        ]


def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> MemoryTools:
    """Register the memory tools into ``registry``."""
    tools = MemoryTools(store)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("memory tools registered (set_name/remember/forget)")
    return tools
