"""The ``diagnostics`` tool: the language server's type errors/warnings for a file.

Fast, inline problem reporting (unresolved imports, type errors, unused names) without running
a build — complements the verify-after-edit loop. Needs a language server for the file's
language; there is no textual fallback (you can't grep for a type error), so without a server it
declines and points at running the linter via ``run_command``. The LSP call is injected
(``diag_fn``) so the formatting is unit-tested without a real server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.lsp import LspError
from autobot.tools.code.symbol_nav import LspManager, _language_for
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger("coder")

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_MAX_DIAGS = 100  # cap problems returned so a very broken file can't flood the turn

# (resolved_file, language) -> the server's diagnostics list, or None to decline (no server).
DiagFn = Callable[["Path", str], "list[dict[str, Any]] | None"]


def _make_diag_fn(manager: LspManager) -> DiagFn:  # pragma: no cover - needs a real server
    """The real backend: sync the file and wait for the server's published diagnostics."""

    def _diag(resolved: Path, language: str) -> list[dict[str, Any]] | None:
        client = manager.client_for(str(resolved.resolve().parent), language)
        if client is None:
            return None
        try:
            uri = resolved.resolve().as_uri()
            client.sync(uri, language, resolved.read_text(encoding="utf-8", errors="replace"))
            return client.await_diagnostics(uri)
        except (LspError, OSError):
            return None

    return _diag


def _format(resolved: Path, diags: list[dict[str, Any]]) -> str:
    """Render diagnostics as ``name:line: [severity] message (source)``, worst first, capped."""
    diags = sorted(diags, key=lambda d: int(d.get("severity", 1)))  # errors (1) before hints (4)
    lines: list[str] = []
    for d in diags[:_MAX_DIAGS]:
        sev = _SEVERITY.get(int(d.get("severity", 1)), "error")
        start = d.get("range", {}).get("start", {})
        row = start.get("line")
        loc = f"{resolved.name}:{int(row) + 1}" if isinstance(row, int) else resolved.name
        source = f" ({d['source']})" if d.get("source") else ""
        lines.append(f"{loc}: [{sev}] {str(d.get('message', '')).strip()}{source}")
    more = f"\n…({len(diags) - _MAX_DIAGS} more)" if len(diags) > _MAX_DIAGS else ""
    return f"{len(diags)} problem(s) in {resolved.name}:\n" + "\n".join(lines) + more


def diagnostics(path: str, broker: AccessBroker, *, diag_fn: DiagFn) -> str:
    """Report the language server's problems for ``path`` (errors/warnings), via ``diag_fn``."""
    if not path:
        return ToolFailure("Which file should I check? Tell me its path.", ErrorCategory.INVALID)
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    if not resolved.is_file():
        return ToolFailure(f"There's no file at {resolved}.", ErrorCategory.NOT_FOUND)
    language = _language_for(str(resolved))
    if language is None:
        return ToolFailure(
            f"No language server is configured for {resolved.name}. Run your build/linter with "
            "run_command to check it instead.",
            ErrorCategory.NOT_FOUND,
        )
    diags = diag_fn(resolved, language)
    if diags is None:
        return ToolFailure(
            f"No language server is installed for {language}. Run your build/linter with "
            "run_command to check this file instead.",
            ErrorCategory.NOT_FOUND,
        )
    if not diags:
        return f"No problems reported for {resolved.name}."
    _log.info("diagnostics name=%r count=%d", resolved.name, len(diags))
    return _format(resolved, diags)


def register_diagnostics_tool(
    registry: ToolRegistry, broker: AccessBroker, manager: LspManager
) -> None:
    """Register the read-only ``diagnostics`` tool, sharing ``manager`` with the other LSP tools."""
    diag_fn = _make_diag_fn(manager)

    def _handler(path: str = "") -> str:
        return diagnostics(path, broker, diag_fn=diag_fn)

    registry.register(
        ToolSpec(
            name="diagnostics",
            description=(
                "Report a file's problems (type errors, unresolved imports, unused names) from a "
                "language server — fast, without running a build. Pass `path`. Great after an edit "
                "to check you didn't break types. Needs a language server (Python/Go/Rust/JS/TS); "
                "if none is installed, run your linter/build with run_command instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to check."},
                },
                "required": ["path"],
            },
            handler=_handler,
            risk=Risk.READ_ONLY,
            ack="Checking for problems.",
        )
    )
