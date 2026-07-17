"""Renderers for MCP server/tool tables and live event lines (pure, theme-driven).

Shared by the REPL ``/mcp`` handler and ``jack mcp``, so both surfaces show the
same tables. Styles come from the theme's semantic tokens — no hard-coded colors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text

# state -> (glyph + words, rich style token from cli/theme.py)
_STATES: dict[str, tuple[str, str]] = {
    "connected": ("● connected", "green"),
    "disconnected": ("○ disconnected", "dim"),
    "pending_consent": ("◌ pending consent", "amber"),
    "denied": ("✕ denied", "red"),
    "error": ("✕ error", "red"),
}

_OAUTH_STAGES: dict[str, str] = {
    "browser_open": "opened your browser — waiting for authorization…",
    "waiting_callback": "waiting for authorization…",
    "callback_received": "authorization received — exchanging token…",
}


def state_label(row: dict[str, Any]) -> tuple[str, str]:
    """The display ``(text, style)`` for a server status row.

    An http server that needs credentials it doesn't have shows ``auth needed``
    (amber) instead of a bare ``disconnected``/``error`` — that's the actionable state.

    Args:
        row: One ``GET /mcp/servers`` status row.

    Returns:
        A ``(text, rich_style)`` pair.
    """
    state = str(row.get("state", "disconnected"))
    needs_auth = (
        state != "connected"
        and str(row.get("auth_type", "none")) != "none"
        and not row.get("secret_present")
    )
    if needs_auth and state in ("disconnected", "error"):
        return ("○ auth needed", "amber")
    return _STATES.get(state, (f"? {state}", "dim"))


def render_servers(payload: dict[str, Any]) -> RenderableType | str:
    """The ``/mcp`` server table (or an actionable message when off/empty/unreachable).

    Args:
        payload: The ``GET /mcp/servers`` response.

    Returns:
        A rich table, or a plain string message.
    """
    if not payload.get("ok"):
        err = str(payload.get("error", "unknown error"))
        if "disabled" in err:
            return "MCP is off — run /mcp on (or jack mcp on) to enable it."
        return f"Couldn't reach MCP: {err}"
    rows = payload.get("servers") or []
    if not rows:
        return "No MCP servers configured. Add one with /mcp add (or jack mcp add)."
    from rich.table import Table
    from rich.text import Text

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("id")
    table.add_column("transport")
    table.add_column("state")
    table.add_column("tools", justify="right")
    table.add_column("egress")
    table.add_column("auth")
    for row in rows:
        text, style = state_label(row)
        egress = str(row.get("egress", "local"))
        table.add_row(
            str(row.get("server", "")),
            str(row.get("transport", "?")),
            Text(text, style=style),
            str(row.get("tool_count", 0)),
            Text(egress, style="amber" if egress == "network" else "dim"),
            Text(str(row.get("auth_type", "none")), style="dim"),
        )
    return table


def render_tools(server_id: str, payload: dict[str, Any]) -> RenderableType | str:
    """The ``/mcp tools <id>`` table (name, risk, enabled, re-consent flag).

    Args:
        server_id: The server whose tools these are (for the empty message).
        payload: The ``GET /mcp/servers/{id}/tools`` response.

    Returns:
        A rich table, or a plain string message.
    """
    if not payload.get("ok"):
        return f"Couldn't list tools: {payload.get('error', 'unknown error')}"
    tools = payload.get("tools") or []
    if not tools:
        return f"{server_id}: no tools (is the server connected? try /mcp)."
    from rich.table import Table
    from rich.text import Text

    _risk_style = {"read_only": "green", "write": "amber", "destructive": "red"}
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("tool")
    table.add_column("risk")
    table.add_column("enabled")
    for t in tools:
        risk = str(t.get("risk", "write"))
        if t.get("pending_reconsent"):
            enabled = Text("blocked ⟳ re-consent", style="red")
        elif t.get("enabled"):
            enabled = Text("on", style="green")
        else:
            enabled = Text("off", style="dim")
        table.add_row(
            str(t.get("name", "")),
            Text(risk, style=_risk_style.get(risk, "dim")),
            enabled,
        )
    if any(t.get("pending_reconsent") for t in tools):
        from rich.console import Group

        note = Text(
            f"blocked tools changed since approval — review with /mcp consent {server_id}",
            style="dim",
        )
        return Group(table, note)
    return table


def render_mcp_event(evt: dict[str, Any]) -> Text | None:
    """One dim transcript line for a live MCP event, or ``None`` to stay quiet.

    Rendered: ``mcp_status`` connected / error / pending_consent, and every
    ``mcp_oauth`` stage. Skipped: disconnect chatter (daemon shutdowns would spam).

    Args:
        evt: An ``mcp_status`` or ``mcp_oauth`` event dict.

    Returns:
        A styled one-liner, or ``None``.
    """
    from rich.text import Text

    server = str(evt.get("server", ""))
    if evt.get("type") == "mcp_oauth":
        stage = _OAUTH_STAGES.get(str(evt.get("stage", "")))
        if stage is None:
            return None
        url = str(evt.get("url", "")).strip()
        extra = (
            f"\n  (nothing opened? visit: {url})"
            if url and evt.get("stage") == "browser_open"
            else ""
        )
        return Text(f"  {server}: {stage}{extra}", style="dim")
    if evt.get("type") != "mcp_status":
        return None
    state = str(evt.get("state", ""))
    if state == "connected":
        n = int(evt.get("tool_count", 0) or 0)
        return Text(f"  ● {server} connected — {n} tools", style="green")
    if state == "pending_consent":
        return Text(f"  ◌ {server} pending consent — /mcp enable {server}", style="amber")
    if state == "error":
        err = str(evt.get("error", "")).strip()
        return Text(f"  ✕ {server} error{': ' + err if err else ''}", style="red")
    return None
