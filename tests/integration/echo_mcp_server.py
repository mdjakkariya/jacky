"""A tiny FastMCP stdio server with one echo tool — a test fixture only."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


# mcp present: FastMCP.tool() is an untyped decorator (strict → [misc]); mcp absent
# (base `uv run mypy`): FastMCP is Any, so the ignore would be unused. Listing both
# codes keeps mypy green in both environments.
@mcp.tool()  # type: ignore[misc, unused-ignore]
def echo(text: str) -> str:
    """Return the input prefixed with 'echo: '."""
    return f"echo: {text}"


@mcp.tool()  # type: ignore[misc, unused-ignore]
def whoami() -> str:
    """Return the value of ECHO_TOKEN from the environment (empty string if absent)."""
    return os.environ.get("ECHO_TOKEN", "")


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
