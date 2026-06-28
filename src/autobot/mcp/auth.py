"""Token injection helpers for MCP server connections.

This module is a pure-function layer with no I/O: the Keychain lookup is injected
as a callable, so unit tests run without touching a real Keychain and so different
secret backends can be substituted trivially. It must not import the ``mcp`` SDK —
auth logic is transport-agnostic and must remain importable without the opt-in extra.

Phase 3 supports ``auth_type="token"`` (static bot/API token) for **stdio** servers:
the token is injected as an environment variable in ``StdioServerParameters(env=...)``,
which is the MCP spec's sanctioned path for stdio credential passing. HTTP bearer-header
injection (also ``auth_type="token"``) is added in Phase 6 alongside OAuth2.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.mcp.config import McpServerConfig


def stdio_env_for(
    cfg: McpServerConfig,
    get_secret: Callable[[str], str | None],
) -> dict[str, str] | None:
    """Build the ``env`` dict for a stdio ``StdioServerParameters``.

    Starts from a copy of ``cfg.env`` (the non-secret vars from ``servers.json``).
    When ``cfg.auth_type == "token"`` and both ``cfg.secret_ref`` and
    ``cfg.token_env`` are set, looks up the token via ``get_secret(cfg.secret_ref)``
    and, if non-``None``, adds ``env[cfg.token_env] = token``.

    Returns ``None`` when the resulting dict is empty — ``StdioServerParameters``
    treats ``env=None`` as "inherit the full parent environment", which is the right
    default for unauthenticated local servers and avoids accidentally stripping
    ``PATH`` / ``HOME`` from the subprocess.

    Args:
        cfg: The server's config descriptor (no secrets stored here).
        get_secret: A callable that returns the secret for a Keychain account name,
            or ``None`` if unset/unavailable. Injected so callers can swap the
            real Keychain for a fake in tests.

    Returns:
        A non-empty ``dict[str, str]`` ready for ``StdioServerParameters(env=...)``,
        or ``None`` to signal "inherit parent env".
    """
    env: dict[str, str] = dict(cfg.env)

    if cfg.auth_type == "token" and cfg.secret_ref is not None and cfg.token_env is not None:
        token = get_secret(cfg.secret_ref)
        if token is not None:
            env[cfg.token_env] = token

    return env if env else None
