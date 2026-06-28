"""MCP integration: connect to MCP servers and expose their tools as ``ToolSpec``s.

This subpackage is the *only* place the ``mcp`` SDK is used, and it is imported
lazily (per the repo's "import heavy runtimes lazily" rule) inside the manager /
session modules added in later phases. The pure layers (``adapter``, ``config``)
import no SDK at all, so they — and their tests — stay fast and dependency-free.
"""

from __future__ import annotations
