"""Tool layer: the registry, tool specs, and built-in tools.

The registry advertises tool schemas to the LLM and dispatches calls back to
Python handlers. Every tool carries a :class:`~autobot.core.types.Risk` level so
the Phase 1 permission gate can decide what to confirm and audit, before any
genuinely-acting tool is added.
"""

from __future__ import annotations

from autobot.tools.builtin import register_builtins
from autobot.tools.registry import ToolRegistry, ToolSpec, default_registry

__all__ = ["ToolRegistry", "ToolSpec", "default_registry", "register_builtins"]
