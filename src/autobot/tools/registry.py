"""Tool registry: schemas out to the model, calls dispatched back to Python.

The registry is the single choke point through which every tool invocation
flows. That is intentional: it is exactly where the Phase 1 permission gate will
sit (risk classification -> confirm destructive -> sandbox -> audit log). For
now all tools are read-only, so :meth:`ToolRegistry.dispatch` runs them directly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from autobot.core.types import Risk, ToolResult

# A tool handler takes JSON-decoded keyword arguments and returns a string.
ToolHandler = Callable[..., str]


class ToolError(Exception):
    """Raised by a handler to report an *expected* failure (e.g. not found, denied).

    The registry maps it to a failed :class:`ToolResult` whose ``content`` is the
    message verbatim — so the model (and the audit log) see ``ok=False`` instead of a
    success-looking string, without the generic ``"tool failed:"`` prefix used for
    unexpected crashes.
    """


class ToolFailure(str):
    """A handler return value that also marks the call failed (``ok=False``).

    Some tools report *expected* failures by **returning** a human-readable message
    instead of raising — so a handler called directly still yields a string and a bad
    tool can never crash the loop. A plain returned string is treated as success, which
    hides those failures from the model-facing ``ok`` flag (and from the harness guards
    that key off it). Wrapping the message in ``ToolFailure`` lets
    :meth:`ToolRegistry.dispatch` set ``ok=False`` **without** changing the
    return-a-string contract: the marker *is* a ``str``, so every existing caller and
    assertion keeps working. Equivalent in effect to raising :class:`ToolError`; prefer
    this for formatted, already-handled failures.
    """

    __slots__ = ()


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
    # Short spoken filler said (voice mode) right before this tool runs, so a slow
    # call isn't dead air. ``None`` → a generic phrase chosen by risk level; ``""``
    # → stay silent (e.g. dismiss, where the reply itself is the goodbye); any text
    # → spoken as-is, with ``{target}`` replaced by the call's main argument
    # (e.g. "Opening {target}." → "Opening Spotify.").
    ack: str | None = None
    # macOS permission this tool needs (see autobot.permissions): "automation",
    # "accessibility", "microphone". The gate refuses (and opens Settings) when it's
    # known to be missing, rather than letting the tool fail deep in AppleScript.
    requires: str | None = None
    # True when this tool sends user data off the device (a network-egress MCP
    # tool). Drives the UI's "↗ sends data off-device" badge and the audit egress
    # note, and — for WRITE-or-higher tools — makes the gate confirm even below the
    # destructive threshold (see PermissionGate, phase 2). False for all local tools.
    network: bool = False
    # True when this tool is part of the always-on "core" set advertised on every
    # turn (the frequent, everyday built-ins). False tools are "gated": advertised
    # only when the ToolSelector judges them relevant to the user's message, which
    # is what keeps per-turn tool context bounded (see autobot.tools.selection). MCP
    # tools are always gated (the adapter never sets this).
    core: bool = False

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
        self._lock = threading.Lock()

    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        """Add a tool. Raises if the name already exists, unless ``replace`` is set.

        Args:
            spec: The tool to register.
            replace: When ``True``, overwrite an existing tool of the same name
                (used by the MCP manager to re-sync a changed tool definition);
                when ``False`` (default), a duplicate raises.

        Raises:
            ValueError: If ``spec.name`` is already registered and ``replace`` is
                ``False``.
        """
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"tool already registered: {spec.name!r}")
            self._tools[spec.name] = spec

    def unregister(self, name: str) -> bool:
        """Remove a registered tool.

        Args:
            name: The tool name to remove.

        Returns:
            ``True`` if a tool was removed, ``False`` if ``name`` was not registered.
            Used when an MCP server is disabled or a tool disappears on re-sync.
        """
        with self._lock:
            return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolSpec | None:
        """Return the spec for ``name``, or ``None`` if it is not registered."""
        with self._lock:
            return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Return every tool's schema, for advertising to the model."""
        with self._lock:
            specs = list(self._tools.values())
        return [spec.to_schema() for spec in specs]

    def specs(self) -> list[ToolSpec]:
        """Return a snapshot of every registered spec (for relevance selection).

        Unlike :meth:`schemas`, this preserves the full :class:`ToolSpec` objects
        (including ``core``/``risk``/``network``), which the :class:`ToolSelector`
        needs to partition core vs. gated and to rank gated tools.
        """
        with self._lock:
            return list(self._tools.values())

    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Execute a registered tool by name.

        The dict lookup is guarded by the registry lock; the handler is invoked
        **after** the lock is released. This is intentional and critical: MCP tool
        handlers block on a ``concurrent.futures.Future`` while the MCP worker
        thread (which also calls ``register``/``unregister``) resolves it. Holding
        the lock during handler execution would deadlock those two threads.

        Tool errors are captured and returned as a failed :class:`ToolResult`
        rather than raised, so a misbehaving tool surfaces to the model as a
        message instead of crashing the loop. A handler signals an expected
        failure either by raising :class:`ToolError` or by returning a
        :class:`ToolFailure` string; both map to ``ok=False``. Any other returned
        string is a success.

        Args:
            name: The tool name requested by the model.
            arguments: JSON-decoded keyword arguments (may be ``None``).

        Returns:
            A :class:`~autobot.core.types.ToolResult`.
        """
        with self._lock:
            spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name=name, content=f"unknown tool: {name!r}", ok=False)
        # Phase 1: insert the permission gate here, keyed on ``spec.risk``.
        try:
            content = spec.handler(**(arguments or {}))
            # A ``ToolFailure`` return marks an expected failure; normalise to a plain
            # ``str`` so the marker never leaks past this boundary into the transcript.
            return ToolResult(
                name=name, content=str(content), ok=not isinstance(content, ToolFailure)
            )
        except ToolError as exc:  # expected failure — report verbatim, ok=False
            return ToolResult(name=name, content=str(exc), ok=False)
        except Exception as exc:  # unexpected — surface, don't crash the loop
            return ToolResult(name=name, content=f"tool failed: {exc}", ok=False)


def default_registry() -> ToolRegistry:
    """Create a registry pre-loaded with the built-in tools."""
    # Imported here to avoid a circular import at module load time.
    from autobot.tools.builtin import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    return registry
