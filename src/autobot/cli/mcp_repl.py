"""The ``/mcp`` REPL command: every verb, driven over the ``Surface`` seam.

Sync verbs (list/tools/tool/on/off/disable) render immediately from the daemon's
answer; interactive verbs (enable's spawn consent, the add wizard, remove's
confirm, auth, consent) park on ``surface.ask`` — the same pinned-input modal the
plan/permission gates use — so the whole module is unit-tested with a fake
surface and fake client functions, no TTY and no daemon.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from autobot.cli import client, mcp_client, mcp_render
from autobot.cli.classify import Segment
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from autobot.cli.surface import Surface

_log = get_logger("cli")

_USAGE = (
    "usage: /mcp [list] · add · enable <id> · disable <id> · remove <id> · tools <id>\n"
    "       /mcp tool <id> <tool> (risk <read_only|write|destructive> | on | off)\n"
    "       /mcp auth <id> [token] · consent <id> · on · off"
)
_YES = frozenset({"yes", "y", "once"})


@dataclass(frozen=True, slots=True)
class Deps:
    """Injectable client functions (defaults are the real HTTP client)."""

    list_servers: Callable[[str], dict[str, Any]] = mcp_client.list_servers
    list_tools: Callable[[str, str], dict[str, Any]] = mcp_client.list_tools
    enable_server: Callable[[str, str], dict[str, Any]] = mcp_client.enable_server
    disable_server: Callable[[str, str], dict[str, Any]] = mcp_client.disable_server
    grant_consent: Callable[[str, str], dict[str, Any]] = mcp_client.grant_consent
    remove_server: Callable[[str, str], dict[str, Any]] = mcp_client.remove_server
    add_server: Callable[[str, dict[str, Any]], dict[str, Any]] = mcp_client.add_server
    set_tool: Callable[..., dict[str, Any]] = mcp_client.set_tool
    auth_start: Callable[[str, str], dict[str, Any]] = mcp_client.auth_start
    post_settings: Callable[[str, dict[str, Any]], dict[str, Any]] = client.post_settings
    post_secret: Callable[[str, str, str], dict[str, Any]] = client.post_secret


def _dim(text: str) -> Any:
    from rich.text import Text

    return Text(text, style="dim")


def _line(text: str, style: str = "") -> Any:
    from rich.text import Text

    return Text(text, style=style)


async def handle(args: str, surface: Surface, *, base_url: str, deps: Deps | None = None) -> None:
    """Dispatch one ``/mcp`` invocation; every outcome is committed to the surface.

    Args:
        args: Everything after ``/mcp`` (may be empty — defaults to ``list``).
        surface: The REPL surface (commits + asks).
        base_url: The coder daemon base URL.
        deps: Injectable client functions (tests pass fakes).
    """
    deps = deps or Deps()
    parts = args.split()
    verb = parts[0] if parts else "list"
    rest = parts[1:]
    _log.debug("mcp command verb=%s", verb)
    if verb == "list":
        surface.commit(mcp_render.render_servers(deps.list_servers(base_url)))
    elif verb == "tools" and len(rest) == 1:
        surface.commit(mcp_render.render_tools(rest[0], deps.list_tools(base_url, rest[0])))
    elif verb == "tool" and len(rest) >= 3:
        await _tool(rest, surface, base_url, deps)
    elif verb == "enable" and len(rest) == 1:
        await _enable(rest[0], surface, base_url, deps)
    elif verb == "disable" and len(rest) == 1:
        res = deps.disable_server(base_url, rest[0])
        surface.commit(_result_line(res, f"{rest[0]} disabled."))
    elif verb == "remove" and len(rest) == 1:
        await _remove(rest[0], surface, base_url, deps)
    elif verb == "add":
        await _add(surface, base_url, deps)
    elif verb == "auth" and rest:
        await _auth(rest, surface, base_url, deps)
    elif verb == "consent" and len(rest) == 1:
        await _consent(rest[0], surface, base_url, deps)
    elif verb in ("on", "off"):
        res = deps.post_settings(base_url, {"allow_mcp": verb == "on"})
        msg = "MCP enabled." if verb == "on" else "MCP disabled — servers disconnected."
        surface.commit(_result_line(res, msg))
    else:
        surface.commit(_dim(_USAGE))


def _result_line(res: dict[str, Any], ok_msg: str) -> Any:
    if res.get("ok"):
        return _line(ok_msg)
    return _line(f"Failed: {res.get('error', 'unknown error')}", "red")


async def _tool(rest: list[str], surface: Surface, base_url: str, deps: Deps) -> None:
    """``/mcp tool <id> <tool> risk <r>`` or ``... on|off``."""
    server_id, tool, action = rest[0], rest[1], rest[2]
    if action == "risk" and len(rest) == 4:
        res = deps.set_tool(base_url, server_id, tool, risk=rest[3])
        surface.commit(_result_line(res, f"{tool} risk → {rest[3]} (reconnecting {server_id})."))
    elif action in ("on", "off"):
        res = deps.set_tool(base_url, server_id, tool, enabled=action == "on")
        surface.commit(_result_line(res, f"{tool} → {action}."))
    else:
        surface.commit(_dim(_USAGE))


async def _enable(server_id: str, surface: Surface, base_url: str, deps: Deps) -> None:
    """Enable; on a pending stdio consent, show the exact command and ask."""
    res = deps.enable_server(base_url, server_id)
    if not res.get("ok"):
        surface.commit(_result_line(res, ""))
        return
    if not res.get("pending_consent"):
        surface.commit(_line(f"{server_id} enabled."))
        return
    command = str(res.get("command", ""))
    args = [str(a) for a in res.get("args") or []]
    shown = " ".join([command, *args]).strip()
    surface.commit(_line(f"{server_id} wants to run:", "amber"))
    surface.commit(_line(f"  {shown}"))
    surface.commit(
        _dim(
            "This spawns a local process with your user permissions. Approval is\n"
            "remembered for this exact command — a changed command asks again."
        )
    )
    ans = await surface.ask(Segment("pending", "Allow this command to run?"))
    if ans.value not in _YES:
        surface.commit(
            _dim(
                f"Denied — {server_id} stays pending; nothing was spawned. "
                f"/mcp enable {server_id} to retry."
            )
        )
        return
    granted = deps.grant_consent(base_url, server_id)
    if not granted.get("ok"):
        surface.commit(_result_line(granted, ""))
        return
    row = granted.get("server") or {}
    surface.commit(
        _line(
            f"● {server_id} {row.get('state', 'connecting')} — {row.get('tool_count', 0)} tools",
            "green",
        )
    )


async def _remove(server_id: str, surface: Surface, base_url: str, deps: Deps) -> None:
    ans = await surface.ask(Segment("pending", f"Remove {server_id} from servers.json?"))
    if ans.value not in _YES:
        surface.commit(_dim("Kept."))
        return
    surface.commit(_result_line(deps.remove_server(base_url, server_id), f"{server_id} removed."))


async def _ask_text(surface: Surface, question: str) -> str | None:
    """One wizard question; ``None`` means the user cancelled (empty answer)."""
    ans = await surface.ask(Segment("input", question))
    return ans.text.strip() if ans.value == "refine" and ans.text.strip() else None


async def _add(surface: Surface, base_url: str, deps: Deps) -> None:
    """The 5-question add wizard (q4 depends on transport; egress is derived)."""
    surface.commit(_dim("Add an MCP server — 5 quick questions (empty answer cancels)."))
    server_id = await _ask_text(surface, "1/5 Server id? (short name, e.g. github)")
    if server_id is None:
        surface.commit(_dim("Cancelled — nothing saved."))
        return
    transport = await _ask_text(surface, "2/5 Transport? (stdio = local process · http = URL)")
    if transport not in ("stdio", "http"):
        surface.commit(_dim("Cancelled — transport must be stdio or http."))
        return
    descriptor: dict[str, Any] = {
        "id": server_id,
        "label": server_id,
        "transport": transport,
        "enabled": False,
        "egress": "local" if transport == "stdio" else "network",
        "default_risk": "write",
    }
    if transport == "stdio":
        cmdline = await _ask_text(surface, "3/5 Command to run? (approved at enable time)")
        if cmdline is None:
            surface.commit(_dim("Cancelled — nothing saved."))
            return
        try:
            argv = shlex.split(cmdline)
        except ValueError:
            msg = "Cancelled — couldn't parse that command line (check your quotes)."
            surface.commit(_dim(msg))
            return
        descriptor["command"], descriptor["args"] = argv[0], argv[1:]
        risk = await _ask_text(surface, "4/5 Risk floor? (read_only | write | destructive)")
        descriptor["default_risk"] = (
            risk if risk in ("read_only", "write", "destructive") else "write"
        )
    else:
        url = await _ask_text(surface, "3/5 Server URL?")
        if url is None:
            surface.commit(_dim("Cancelled — nothing saved."))
            return
        descriptor["url"] = url
        auth = await _ask_text(surface, "4/5 Auth? (none | token | oauth)")
        auth = auth if auth in ("none", "token", "oauth") else "none"
        descriptor["auth"] = {"type": auth}
        if auth == "token":
            descriptor["secret_ref"] = f"mcp.{server_id}.token"
    summary = descriptor.get("command", descriptor.get("url", ""))
    surface.commit(_line(f"5/5 Save {server_id}? ({descriptor['transport']} · {summary})"))
    ans = await surface.ask(Segment("pending", f"Save {server_id}?"))
    if ans.value not in _YES:
        surface.commit(_dim("Cancelled — nothing saved."))
        return
    res = deps.add_server(base_url, descriptor)
    surface.commit(
        _result_line(res, f"{server_id} saved (disabled) — /mcp enable {server_id} to connect.")
    )


async def _auth(rest: list[str], surface: Surface, base_url: str, deps: Deps) -> None:
    """OAuth kick-off, or masked token entry (``auth <id> token``)."""
    server_id = rest[0]
    if len(rest) > 1 and rest[1] == "token":
        ans = await surface.ask(Segment("secret", f"Paste token for {server_id} (input hidden)"))
        if ans.value != "refine" or not ans.text:
            surface.commit(_dim("Cancelled — no token stored."))
            return
        res = deps.post_secret(base_url, f"mcp.{server_id}.token", ans.text)
        if not res.get("ok"):
            surface.commit(_result_line(res, ""))
            return
        surface.commit(
            _line(f"✓ stored in Keychain as mcp.{server_id}.token — never written to disk", "green")
        )
        deps.enable_server(base_url, server_id)  # reconnect with the new credential
        return
    res = deps.auth_start(base_url, server_id)
    if not res.get("ok"):
        surface.commit(_result_line(res, ""))
        return
    surface.commit(
        _dim(f"Starting OAuth for {server_id} — your browser will open; progress shows here.")
    )


async def _consent(server_id: str, surface: Surface, base_url: str, deps: Deps) -> None:
    """Grant a pending spawn consent, or re-approve rug-pull-blocked tools."""
    tools = deps.list_tools(base_url, server_id)
    blocked = [t for t in tools.get("tools") or [] if t.get("pending_reconsent")]
    for t in blocked:
        surface.commit(_line(f"changed since approval: {t.get('name')}", "amber"))
    prompt = (
        f"Re-approve {len(blocked)} changed tool(s) on {server_id}?"
        if blocked
        else f"Approve {server_id}?"
    )
    ans = await surface.ask(Segment("pending", prompt))
    if ans.value not in _YES:
        surface.commit(_dim("Left as-is."))
        return
    surface.commit(_result_line(deps.grant_consent(base_url, server_id), f"{server_id} approved."))
