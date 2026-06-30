"""A tiny FastMCP stdio server with one echo tool — a test fixture only."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


# FastMCP's tool() decorator trips strict mypy differently per environment: with the mcp
# extra installed it's [misc]; without it (base `uv run mypy` / CI, where mcp is Any) the
# decorator is untyped → [untyped-decorator]. List both codes (+ unused-ignore) so mypy
# stays green whether or not the extra is present.
@mcp.tool()  # type: ignore[misc, untyped-decorator, unused-ignore]
def echo(text: str) -> str:
    """Return the input prefixed with 'echo: '."""
    return f"echo: {text}"


@mcp.tool()  # type: ignore[misc, untyped-decorator, unused-ignore]
def whoami() -> str:
    """Return the value of ECHO_TOKEN from the environment (empty string if absent)."""
    return os.environ.get("ECHO_TOKEN", "")


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
