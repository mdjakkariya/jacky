"""``jack mcp`` — script-friendly MCP management from a plain shell.

Same verbs and client layer as the REPL's ``/mcp`` (``cli/mcp_repl.py``), with
flags instead of a wizard, ``--yes`` for non-interactive consent (CI), and
``--json`` for raw payloads. Prints via an injectable ``out`` and prompts via
injectable ``ask``/``ask_secret``, so every path is unit-tested without a TTY.
"""

from __future__ import annotations

import getpass
import json
from typing import TYPE_CHECKING, Any

from autobot.cli import mcp_render
from autobot.cli.mcp_repl import Deps
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger("cli")

_USAGE = """usage: jack mcp <verb>
  list [--json]                     all servers + state
  add <id> (--stdio --command CMD [--args ...] | --http --url URL)
           [--auth none|token|oauth] [--risk read_only|write|destructive]
  enable <id> [--yes]               connect (asks stdio spawn consent; --yes grants)
  disable <id> · remove <id> [--yes]
  tools <id> [--json]               tool list with risk + enabled
  tool <id> <tool> (--risk R | --on | --off)
  auth <id> [--token]               OAuth browser flow, or paste a token
  consent <id> [--yes]              grant pending spawn/re-consent
  on | off                          flip the global allow_mcp setting"""


def _print_renderable(renderable: Any, out: Callable[[str], None]) -> None:
    """Render a rich table (or pass a string through) to ``out``."""
    if isinstance(renderable, str):
        out(renderable)
        return
    from io import StringIO

    from rich.console import Console

    from autobot.cli.theme import jack_theme

    buf = StringIO()
    Console(file=buf, theme=jack_theme(), width=100).print(renderable)
    out(buf.getvalue().rstrip("\n"))


def _fail(res: dict[str, Any], out: Callable[[str], None]) -> int:
    out(f"error: {res.get('error', 'unknown error')}")
    return 1


def run(
    argv: list[str],
    *,
    base_url: str,
    deps: Deps | None = None,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
) -> int:
    """Dispatch one ``jack mcp`` invocation.

    Args:
        argv: Everything after ``jack mcp``.
        base_url: The coder daemon base URL (daemon already ensured by the caller).
        deps: Injectable client functions (tests pass fakes).
        ask: Plain-text prompt (consent questions).
        ask_secret: Hidden prompt (tokens).
        out: Line printer.

    Returns:
        0 on success, 1 on a failed/denied operation, 2 on a usage error.
    """
    deps = deps or Deps()
    if not argv:
        out(_USAGE)
        return 2
    verb, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    yes = "--yes" in rest
    rest = [a for a in rest if a != "--yes"]
    _log.debug("jack mcp verb=%s", verb)

    if verb == "list" and not rest:
        payload = deps.list_servers(base_url)
        if as_json:
            out(json.dumps(payload))
            return 0 if payload.get("ok") else 1
        _print_renderable(mcp_render.render_servers(payload), out)
        return 0 if payload.get("ok") else 1
    if verb == "tools" and len(rest) == 1:
        payload = deps.list_tools(base_url, rest[0])
        if as_json:
            out(json.dumps(payload))
        else:
            _print_renderable(mcp_render.render_tools(rest[0], payload), out)
        return 0 if payload.get("ok") else 1
    if verb == "add" and rest:
        return _add(rest, deps, base_url, out)
    if verb == "enable" and len(rest) == 1:
        return _enable(rest[0], yes, deps, base_url, ask, out)
    if verb == "disable" and len(rest) == 1:
        res = deps.disable_server(base_url, rest[0])
        return 0 if res.get("ok") else _fail(res, out)
    if verb == "remove" and len(rest) == 1:
        if not yes and ask(f"Remove {rest[0]}? [y/N] ").strip().lower() not in ("y", "yes"):
            out("kept.")
            return 1
        res = deps.remove_server(base_url, rest[0])
        return 0 if res.get("ok") else _fail(res, out)
    if verb == "tool" and len(rest) >= 3:
        return _tool(rest, deps, base_url, out)
    if verb == "auth" and rest:
        return _auth(rest, deps, base_url, ask_secret, out)
    if verb == "consent" and len(rest) == 1:
        if not yes and ask(f"Approve {rest[0]}? [y/N] ").strip().lower() not in ("y", "yes"):
            out("left as-is.")
            return 1
        res = deps.grant_consent(base_url, rest[0])
        return 0 if res.get("ok") else _fail(res, out)
    if verb in ("on", "off") and not rest:
        res = deps.post_settings(base_url, {"allow_mcp": verb == "on"})
        out(f"MCP {'enabled' if verb == 'on' else 'disabled'}.")
        return 0 if res.get("ok") else 1
    out(_USAGE)
    return 2


def _flag_value(rest: list[str], flag: str) -> str | None:
    """The value following ``flag`` in ``rest``, or ``None``."""
    if flag in rest:
        i = rest.index(flag)
        if i + 1 < len(rest):
            return rest[i + 1]
    return None


def _add(rest: list[str], deps: Deps, base_url: str, out: Callable[[str], None]) -> int:
    server_id = rest[0]
    descriptor: dict[str, Any] = {
        "id": server_id,
        "label": server_id,
        "enabled": False,
        "default_risk": _flag_value(rest, "--risk") or "write",
    }
    auth = _flag_value(rest, "--auth") or "none"
    descriptor["auth"] = {"type": auth}
    if auth == "token":
        descriptor["secret_ref"] = f"mcp.{server_id}.token"
    if "--stdio" in rest:
        command = _flag_value(rest, "--command")
        if not command:
            out("error: --stdio needs --command")
            return 2
        descriptor["transport"] = "stdio"
        descriptor["egress"] = "local"
        descriptor["command"] = command
        if "--args" in rest:
            i = rest.index("--args")
            args: list[str] = []
            for a in rest[i + 1 :]:
                if a.startswith("--"):
                    break
                args.append(a)
            descriptor["args"] = args
    elif "--http" in rest:
        url = _flag_value(rest, "--url")
        if not url:
            out("error: --http needs --url")
            return 2
        descriptor["transport"] = "http"
        descriptor["egress"] = "network"
        descriptor["url"] = url
    else:
        out("error: add needs --stdio or --http")
        return 2
    res = deps.add_server(base_url, descriptor)
    if not res.get("ok"):
        return _fail(res, out)
    out(f"{server_id} saved (disabled) — jack mcp enable {server_id}")
    return 0


def _enable(
    server_id: str,
    yes: bool,
    deps: Deps,
    base_url: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> int:
    res = deps.enable_server(base_url, server_id)
    if not res.get("ok"):
        return _fail(res, out)
    if not res.get("pending_consent"):
        out(f"{server_id} enabled.")
        return 0
    shown = " ".join([str(res.get("command", "")), *[str(a) for a in res.get("args") or []]])
    shown = shown.strip()
    if yes:
        out(f"consent granted via --yes for: {shown}")
    elif ask(f"{server_id} wants to run:\n  {shown}\nAllow? [y/N] ").strip().lower() not in (
        "y",
        "yes",
    ):
        out(f"denied — {server_id} stays pending; nothing was spawned.")
        return 1
    granted = deps.grant_consent(base_url, server_id)
    if not granted.get("ok"):
        return _fail(granted, out)
    row = granted.get("server") or {}
    out(f"{server_id} {row.get('state', 'connecting')} — {row.get('tool_count', 0)} tools")
    return 0


def _tool(rest: list[str], deps: Deps, base_url: str, out: Callable[[str], None]) -> int:
    server_id, tool = rest[0], rest[1]
    risk = _flag_value(rest, "--risk")
    if risk is not None:
        res = deps.set_tool(base_url, server_id, tool, risk=risk)
    elif "--on" in rest:
        res = deps.set_tool(base_url, server_id, tool, enabled=True)
    elif "--off" in rest:
        res = deps.set_tool(base_url, server_id, tool, enabled=False)
    else:
        out(_USAGE)
        return 2
    return 0 if res.get("ok") else _fail(res, out)


def _auth(
    rest: list[str],
    deps: Deps,
    base_url: str,
    ask_secret: Callable[[str], str],
    out: Callable[[str], None],
) -> int:
    server_id = rest[0]
    if "--token" in rest:
        token = ask_secret(f"Token for {server_id} (hidden): ").strip()
        if not token:
            out("cancelled — no token stored.")
            return 1
        res = deps.post_secret(base_url, f"mcp.{server_id}.token", token)
        if not res.get("ok"):
            return _fail(res, out)
        out(f"stored in Keychain as mcp.{server_id}.token")
        deps.enable_server(base_url, server_id)
        return 0
    res = deps.auth_start(base_url, server_id)
    if not res.get("ok"):
        return _fail(res, out)
    out(f"OAuth started for {server_id} — complete it in your browser, then: jack mcp list")
    return 0
