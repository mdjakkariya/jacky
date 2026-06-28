"""Declarative MCP server descriptors, persisted as JSON (config only, no secrets).

Mirrors the ``settings.json`` split: this file holds connection config; the
Keychain holds tokens (account names like ``mcp.<id>.token``). Adding a server is
editing ``~/.autobot/mcp/servers.json`` (or using the Settings view) — never code.
Robust by design: a missing or malformed file yields ``{}`` and a server with an
unusable transport is skipped, so a hand-edited file can never crash startup.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MCP_CONFIG_PATH = "~/.autobot/mcp/servers.json"

_VALID_TRANSPORTS = {"stdio", "http"}


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One configured MCP server (see ``docs/plans/mcp-integration-design.md`` §5).

    Config only — never secrets. ``secret_ref`` is a Keychain *account name*, not a
    value. ``egress`` is ``"network"`` (sends data off-device, the disclosed
    exception) or ``"local"`` (on-device stdio). ``default_risk`` is the floor for
    this server's tools; ``tool_risk_overrides`` adjusts individual tools.
    """

    id: str
    label: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    auth_type: str = "none"
    token_env: str | None = None
    secret_ref: str | None = None
    enabled: bool = False
    egress: str = "local"
    default_risk: str = "write"
    tool_allow: tuple[str, ...] = ()
    tool_deny: tuple[str, ...] = ()
    tool_risk_overrides: dict[str, str] = field(default_factory=dict)


def _opt_str(value: Any) -> str | None:
    """A non-empty string, or ``None``."""
    return value if isinstance(value, str) and value else None


def _str_tuple(value: Any) -> tuple[str, ...]:
    """A tuple of strings from a JSON list (``()`` if not a list)."""
    return tuple(str(x) for x in value) if isinstance(value, list) else ()


def _str_map(value: Any) -> dict[str, str]:
    """A ``str->str`` map from a JSON object (``{}`` if not an object)."""
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _coerce_server(server_id: str, data: dict[str, Any]) -> McpServerConfig | None:
    """Build one ``McpServerConfig`` from a raw JSON object; ``None`` if unusable."""
    transport = str(data.get("transport", "")).strip()
    if transport not in _VALID_TRANSPORTS:
        return None
    auth = data.get("auth")
    auth_type = str(auth.get("type", "none")) if isinstance(auth, dict) else "none"
    return McpServerConfig(
        id=server_id,
        label=str(data.get("label", server_id)),
        transport=transport,
        command=_opt_str(data.get("command")),
        args=_str_tuple(data.get("args")),
        env=_str_map(data.get("env")),
        url=_opt_str(data.get("url")),
        auth_type=auth_type,
        token_env=_opt_str(data.get("token_env")),
        secret_ref=_opt_str(data.get("secret_ref")),
        enabled=bool(data.get("enabled", False)),
        egress=str(data.get("egress", "local")),
        default_risk=str(data.get("default_risk", "write")),
        tool_allow=_str_tuple(data.get("tool_allow")),
        tool_deny=_str_tuple(data.get("tool_deny")),
        tool_risk_overrides=_str_map(data.get("tool_risk_overrides")),
    )


def _to_json(cfg: McpServerConfig) -> dict[str, Any]:
    """Serialize a config back to the ``servers.json`` descriptor shape (no id key)."""
    return {
        "label": cfg.label,
        "transport": cfg.transport,
        "command": cfg.command,
        "args": list(cfg.args),
        "env": dict(cfg.env),
        "url": cfg.url,
        "auth": {"type": cfg.auth_type},
        "token_env": cfg.token_env,
        "secret_ref": cfg.secret_ref,
        "enabled": cfg.enabled,
        "egress": cfg.egress,
        "default_risk": cfg.default_risk,
        "tool_allow": list(cfg.tool_allow),
        "tool_deny": list(cfg.tool_deny),
        "tool_risk_overrides": dict(cfg.tool_risk_overrides),
    }


def load_mcp_config(path: str | Path = DEFAULT_MCP_CONFIG_PATH) -> dict[str, McpServerConfig]:
    """Load all configured servers, keyed by id. ``{}`` if missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, McpServerConfig] = {}
    for server_id, raw in servers.items():
        if isinstance(raw, dict):
            cfg = _coerce_server(str(server_id), raw)
            if cfg is not None:
                out[str(server_id)] = cfg
    return out


def save_mcp_config(
    servers: dict[str, McpServerConfig], path: str | Path = DEFAULT_MCP_CONFIG_PATH
) -> None:
    """Persist servers to ``servers.json`` (0600), creating parent dirs as needed."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": {sid: _to_json(cfg) for sid, cfg in servers.items()}}
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):  # best effort on exotic filesystems
        p.chmod(0o600)
