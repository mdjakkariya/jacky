"""HTTP client for the daemon's ``/mcp/*`` endpoints (one function per route).

Mirrors ``cli/client.py``: dependency-free ``urllib`` transport with every network
function injectable, so both surfaces (the REPL handler and ``jack mcp``) are
unit-tested with fakes. Every function returns the endpoint's JSON payload, or a
soft ``{"ok": False, "error": str}`` on transport failure — never raises.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from autobot.cli.client import _get_json, _post

if TYPE_CHECKING:
    from collections.abc import Callable

_TIMEOUT_S = 10.0


def _delete(url: str, timeout: float) -> dict[str, Any]:  # pragma: no cover - real network
    """DELETE ``url`` and return the parsed JSON body."""
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        parsed: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    return parsed


def _soft(exc: Exception) -> dict[str, Any]:
    """The uniform transport-failure payload."""
    return {"ok": False, "error": str(exc)}


def list_servers(base_url: str, *, get: Callable[[str, float], Any] = _get_json) -> dict[str, Any]:
    """``GET /mcp/servers`` — status rows for every configured server."""
    try:
        data = get(f"{base_url}/mcp/servers", _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)
    return data if isinstance(data, dict) else {"ok": False, "error": "bad response"}


def add_server(
    base_url: str,
    descriptor: dict[str, Any],
    *,
    post: Callable[..., dict[str, Any]] = _post,
) -> dict[str, Any]:
    """``POST /mcp/servers`` — add or update one server descriptor."""
    try:
        return post(f"{base_url}/mcp/servers", descriptor, _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def remove_server(
    base_url: str,
    server_id: str,
    *,
    delete: Callable[[str, float], dict[str, Any]] = _delete,
) -> dict[str, Any]:
    """``DELETE /mcp/servers/{id}`` — remove a configured server."""
    try:
        return delete(f"{base_url}/mcp/servers/{server_id}", _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def enable_server(
    base_url: str, server_id: str, *, post: Callable[..., dict[str, Any]] = _post
) -> dict[str, Any]:
    """``POST /mcp/servers/{id}/enable`` — may report ``pending_consent`` + command/args."""
    try:
        return post(f"{base_url}/mcp/servers/{server_id}/enable", {}, _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def disable_server(
    base_url: str, server_id: str, *, post: Callable[..., dict[str, Any]] = _post
) -> dict[str, Any]:
    """``POST /mcp/servers/{id}/disable``."""
    try:
        return post(f"{base_url}/mcp/servers/{server_id}/disable", {}, _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def grant_consent(
    base_url: str, server_id: str, *, post: Callable[..., dict[str, Any]] = _post
) -> dict[str, Any]:
    """``POST /mcp/servers/{id}/consent`` — grant spawn/re-consent and reconnect."""
    try:
        return post(f"{base_url}/mcp/servers/{server_id}/consent", {}, 30.0)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def auth_start(
    base_url: str, server_id: str, *, post: Callable[..., dict[str, Any]] = _post
) -> dict[str, Any]:
    """``POST /mcp/servers/{id}/auth/start`` — kick off the OAuth browser flow."""
    try:
        return post(f"{base_url}/mcp/servers/{server_id}/auth/start", {}, 30.0)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)


def list_tools(
    base_url: str, server_id: str, *, get: Callable[[str, float], Any] = _get_json
) -> dict[str, Any]:
    """``GET /mcp/servers/{id}/tools`` — the cached all-tools snapshot."""
    try:
        data = get(f"{base_url}/mcp/servers/{server_id}/tools", _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)
    return data if isinstance(data, dict) else {"ok": False, "error": "bad response"}


def set_tool(
    base_url: str,
    server_id: str,
    tool: str,
    *,
    risk: str | None = None,
    enabled: bool | None = None,
    post: Callable[..., dict[str, Any]] = _post,
) -> dict[str, Any]:
    """``POST /mcp/servers/{id}/tools/{tool}`` — risk override and/or enable toggle."""
    body: dict[str, Any] = {}
    if risk is not None:
        body["risk"] = risk
    if enabled is not None:
        body["enabled"] = enabled
    try:
        return post(f"{base_url}/mcp/servers/{server_id}/tools/{tool}", body, _TIMEOUT_S)
    except (OSError, urllib.error.URLError) as exc:
        return _soft(exc)
