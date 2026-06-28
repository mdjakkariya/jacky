"""A tiny FastMCP stdio server with one echo tool — a test fixture only."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


# mcp present: FastMCP.tool() is an untyped decorator (strict → [misc]); mcp absent
# (base `uv run mypy`): FastMCP is Any, so the ignore would be unused. Listing both
# codes keeps mypy green in both environments.
@mcp.tool()  # type: ignore[misc, unused-ignore]
def echo(text: str) -> str:
    """Return the input prefixed with 'echo: '."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
