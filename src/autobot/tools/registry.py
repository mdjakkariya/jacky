"""Tool registry: schemas out to the model, calls dispatched back to Python.

The registry is the single choke point through which every tool invocation
flows. That is intentional: it is exactly where the Phase 1 permission gate will
sit (risk classification -> confirm destructive -> sandbox -> audit log). For
now all tools are read-only, so :meth:`ToolRegistry.dispatch` runs them directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from autobot.core.types import Risk, ToolResult

# A tool handler takes JSON-decoded keyword arguments and returns a string.
ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A registered tool: its advertised schema, handler, and risk level."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk: Risk = Risk.READ_ONLY
    # A plain, human-friendly question shown/spoken when confirming this action
    # (e.g. "Empty the Trash? This permanently deletes everything in it."). Falls
    # back to a generic prompt when unset.
    confirm_prompt: str | None = None

    def to_schema(self) -> dict[str, Any]:
        """Render the OpenAI/Ollama-style ``function`` schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Holds the available tools and dispatches calls to their handlers."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Add a tool. Raises if a tool with the same name already exists.

        Args:
            spec: The tool to register.

        Raises:
            ValueError: If ``spec.name`` is already registered.
        """
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """Return the spec for ``name``, or ``None`` if it is not registered."""
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Return every tool's schema, for advertising to the model."""
        return [spec.to_schema() for spec in self._tools.values()]

    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Execute a registered tool by name.

        Tool errors are captured and returned as a failed :class:`ToolResult`
        rather than raised, so a misbehaving tool surfaces to the model as a
        message instead of crashing the loop.

        Args:
            name: The tool name requested by the model.
            arguments: JSON-decoded keyword arguments (may be ``None``).

        Returns:
            A :class:`~autobot.core.types.ToolResult`.
        """
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name=name, content=f"unknown tool: {name!r}", ok=False)
        # Phase 1: insert the permission gate here, keyed on ``spec.risk``.
        try:
            content = spec.handler(**(arguments or {}))
            return ToolResult(name=name, content=content, ok=True)
        except Exception as exc:  # surface any tool error to the model, don't crash
            return ToolResult(name=name, content=f"tool failed: {exc}", ok=False)


def default_registry() -> ToolRegistry:
    """Create a registry pre-loaded with the built-in tools."""
    # Imported here to avoid a circular import at module load time.
    from autobot.tools.builtin import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    return registry
