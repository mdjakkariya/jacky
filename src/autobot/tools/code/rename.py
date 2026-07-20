"""The ``rename_symbol`` tool: semantic, cross-file rename via a language server (WRITE).

There is **no textual fallback** — a grep-and-replace rename is unsafe (it can't tell a real
reference from coincidental text), which is exactly why LSP rename matters. Without a server
for the file's language we decline and point at the manual tools, rather than risk a wrong
rename. The LSP call is injected (``rename_fn``) so the whole WorkspaceEdit→files flow is
unit-tested without a real server; only the default ``rename_fn`` touches a process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.lsp import LspError, apply_text_edits, uri_to_path, workspace_edit_files
from autobot.tools.code.symbol_nav import LspManager, _column_of, _language_for
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger("coder")

_MAX_RENAME_FILES = 200  # cap files touched by one rename

# (resolved_file, 0-based line, 0-based col, new_name) -> WorkspaceEdit, or None to decline.
RenameFn = Callable[["Path", int, int, str], "dict[str, Any] | None"]


def _make_rename_fn(manager: LspManager) -> RenameFn:  # pragma: no cover - needs a real server
    """The real rename backend: language server for the file's language, or ``None`` to decline."""

    def _rename(resolved: Path, line0: int, col: int, new_name: str) -> dict[str, Any] | None:
        language = _language_for(str(resolved))
        if language is None:
            return None
        client = manager.client_for(str(resolved.resolve().parent), language)
        if client is None:
            return None
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
            client.sync(resolved.resolve().as_uri(), language, text)
            return client.rename(resolved.resolve().as_uri(), line0, col, new_name)
        except (LspError, OSError):
            return None

    return _rename


def rename_symbol(
    name: str,
    path: str,
    new_name: str,
    broker: AccessBroker,
    *,
    line: int,
    rename_fn: RenameFn,
) -> str:
    """Rename ``name`` (seen at ``path``:``line``) to ``new_name`` everywhere, via ``rename_fn``."""
    if not name or not new_name or not path:
        return ToolFailure(
            "Tell me the symbol `name`, its `path`, the `line`, and the `new_name`.",
            ErrorCategory.INVALID,
        )
    if not line:
        return ToolFailure(
            "Rename needs the 1-based `line` where the symbol appears.", ErrorCategory.INVALID
        )
    try:
        resolved = broker.ensure(path, write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return ToolFailure(str(exc), ErrorCategory.DENIED)
    if not resolved.is_file():
        return ToolFailure(f"There's no file at {resolved}.", ErrorCategory.NOT_FOUND)
    try:
        rows = resolved.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as exc:
        return ToolFailure(f"I couldn't read {resolved.name}: {exc}", ErrorCategory.UNREADABLE)
    if not (1 <= line <= len(rows)):
        return ToolFailure(f"Line {line} is outside {resolved.name}.", ErrorCategory.INVALID)
    col = _column_of(rows[line - 1], name)
    if col is None:
        return ToolFailure(
            f"Couldn't find {name!r} on line {line} of {resolved.name}.", ErrorCategory.NOT_FOUND
        )
    edit = rename_fn(resolved, line - 1, col, new_name)
    if edit is None:
        return ToolFailure(
            "Rename needs a language server for this file's language, and none is available. A "
            "textual rename isn't safe, so I won't guess — install a server, or change the call "
            "sites yourself with edit_file/multi_patch once you've verified them.",
            ErrorCategory.NOT_FOUND,
        )
    files = workspace_edit_files(edit)
    if not files:
        return f"The language server produced no rename for {name!r}."
    # Phase 1: resolve + read + apply every file IN MEMORY. If any file can't be accessed (jail
    # or a declined grant) or read, abort writing nothing — a rename must be all-or-nothing, not
    # a half-renamed tree that no longer compiles.
    planned: list[tuple[Path, str]] = []  # (resolved_target, new_text)
    for uri, edits in list(files.items())[:_MAX_RENAME_FILES]:
        try:
            target = broker.ensure(uri_to_path(uri), write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return ToolFailure(f"Rename aborted, no files changed: {exc}", ErrorCategory.DENIED)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolFailure(
                f"Rename aborted, no files changed — couldn't read {target.name}: {exc}",
                ErrorCategory.UNREADABLE,
            )
        new_text = apply_text_edits(text, edits)
        if new_text != text:
            planned.append((target, new_text))
    if not planned:
        return f"No change needed to rename {name!r} to {new_name!r}."
    # Phase 2: write them all. A mid-write OS error is rare; report it honestly (the turn
    # checkpoint can roll back) rather than claim a clean success.
    changed: list[str] = []
    for target, new_text in planned:
        try:
            target.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return ToolFailure(
                f"Partly renamed ({len(changed)} of {len(planned)} files written) then failed on "
                f"{target.name}: {exc}. Use the checkpoint to roll back.",
                ErrorCategory.UNREADABLE,
            )
        changed.append(target.name)
    _log.info("rename_symbol %r -> %r files=%d", name, new_name, len(changed))
    return f"Renamed {name!r} → {new_name!r} across {len(changed)} file(s): " + ", ".join(
        sorted(set(changed))
    )


def register_rename_tool(registry: ToolRegistry, broker: AccessBroker, manager: LspManager) -> None:
    """Register the WRITE ``rename_symbol`` tool, sharing ``manager`` with the symbol tool."""
    rename_fn = _make_rename_fn(manager)

    def _handler(name: str = "", path: str = "", line: int = 0, new_name: str = "") -> str:
        try:
            line_no = int(line or 0)
        except (TypeError, ValueError):
            line_no = 0
        return rename_symbol(name, path, new_name, broker, line=line_no, rename_fn=rename_fn)

    registry.register(
        ToolSpec(
            name="rename_symbol",
            description=(
                "Rename a symbol everywhere it's used — safely and scope-aware, across files, via "
                "a language server. Pass the symbol `name`, the `path` and 1-based `line` where "
                "you see it, and the `new_name`. Needs a language server for the file's language "
                "(Python/Go/Rust/JS/TS); if none is installed it declines rather than doing an "
                "unsafe textual replace. Prefer this over hand-editing call sites for a rename."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Current symbol name."},
                    "path": {"type": "string", "description": "File where you see the symbol."},
                    "line": {"type": "integer", "description": "1-based line where it appears."},
                    "new_name": {"type": "string", "description": "The new name."},
                },
                "required": ["name", "path", "line", "new_name"],
            },
            handler=_handler,
            risk=Risk.WRITE,
            ack="Renaming the symbol.",
        )
    )
