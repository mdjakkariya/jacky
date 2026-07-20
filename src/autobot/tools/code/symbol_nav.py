"""The ``symbol`` tool: semantic go-to-definition / find-references, LSP-backed with fallback.

Language-agnostic contract (see epic #105): given a symbol ``name`` and where the model saw
it (``path`` + 1-based ``line``), return its definition or references. When a language server
is registered for the file's language *and* its binary is on PATH, the answer is **semantic**
(via :mod:`autobot.tools.code.lsp`). Otherwise — unsupported language, or no server installed —
it transparently **falls back** to the textual approach (``grep``, which already uses ripgrep
+ noise pruning). So the tool works for every language on day one and only gets sharper as
servers are added; it never removes a capability.

Phase 1 registers a Python server backend; more languages are Phase 2 (just extend the maps).
The real subprocess/spawn paths are exercised only where a server is installed, so they are
kept behind defensive fallbacks and marked ``pragma: no cover``; the fallback and dispatch are
unit-tested offline.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.lsp import LspClient, LspError, StdioTransport, uri_to_path
from autobot.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger("coder")

_ACTIONS = ("definition", "references", "hover")
_MAX_LOCS = 50  # cap locations returned so a widely-used symbol can't flood the turn

# LSP language id -> candidate server argv; the first whose argv[0] is on PATH is used. Any
# language/extension not covered here transparently uses the textual fallback (grep).
_TS_SERVER = [["typescript-language-server", "--stdio"]]
_SERVERS: dict[str, list[list[str]]] = {
    "python": [
        ["basedpyright-langserver", "--stdio"],
        ["pyright-langserver", "--stdio"],
        ["pylsp"],
    ],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "typescript": _TS_SERVER,
    "typescriptreact": _TS_SERVER,
    "javascript": _TS_SERVER,
    "javascriptreact": _TS_SERVER,
}
_EXT_LANG: dict[str, str] = {  # file extension -> LSP language id
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
}

_FALLBACK_NOTE = "(textual fallback — install a language server for precise, semantic results)\n"


def _language_for(path: str) -> str | None:
    """The LSP language id for ``path``'s extension, or ``None`` if unsupported."""
    return _EXT_LANG.get(Path(path).suffix)


def _server_argv(language: str) -> list[str] | None:
    """The first configured server argv for ``language`` whose binary is on PATH, else ``None``."""
    for argv in _SERVERS.get(language, []):
        if shutil.which(argv[0]):
            return argv
    return None


def _column_of(line_text: str, name: str) -> int | None:
    """0-based column of ``name`` as a whole word in ``line_text`` (``None`` if absent)."""
    match = re.search(rf"\b{re.escape(name)}\b", line_text)
    return match.start() if match else None


class LspManager:
    """Spawns and reuses one language server per (workspace root, language)."""

    def __init__(self) -> None:
        # (root, language) -> (client, process). Keeping the proc lets us check liveness and
        # kill it on failure/shutdown (no leaked process or reader thread).
        self._clients: dict[tuple[str, str], tuple[LspClient, Any]] = {}

    def client_for(self, root: str, language: str) -> LspClient | None:  # pragma: no cover - spawns
        """A ready client for ``language`` under ``root``, or ``None`` if no server / on failure.

        Reuses a live server; a server that has died is dropped and respawned. On any failure the
        just-spawned process is killed so neither it nor its reader thread leaks.
        """
        import subprocess

        key = (root, language)
        existing = self._clients.get(key)
        if existing is not None:
            client, proc = existing
            if proc.poll() is None:  # still running — reuse it
                return client
            del self._clients[key]  # the server died; fall through and respawn a fresh one
        argv = _server_argv(language)
        if argv is None:
            return None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            _log.warning("lsp server for %s failed to spawn; falling back", language, exc_info=True)
            return None
        try:
            client = LspClient(StdioTransport(proc))
            client.initialize(Path(root).resolve().as_uri())
        except (LspError, OSError, ValueError):
            _log.warning(
                "lsp server for %s failed to init; killing + falling back", language, exc_info=True
            )
            with contextlib.suppress(Exception):
                proc.kill()  # don't leak the process or its blocked reader thread
            return None
        self._clients[key] = (client, proc)
        _log.info("lsp server started language=%s argv=%s", language, argv[0])
        return client

    def shutdown_all(self) -> None:  # pragma: no cover - real subprocess boundary
        """Shut down every server (best-effort)."""
        for client, proc in self._clients.values():
            client.shutdown()
            with contextlib.suppress(Exception):
                proc.terminate()
        self._clients.clear()


def _hover_text(result: Any) -> str:
    """Flatten an LSP hover result (str | MarkupContent | MarkedString | list) to plain text."""

    def one(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return str(content.get("value", ""))
        return ""

    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    parts = [one(c) for c in contents] if isinstance(contents, list) else [one(contents)]
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip()


def _loc_to_line(loc: dict[str, Any], root: Path) -> str:
    """Render an LSP Location as ``relpath:line`` (1-based), best-effort."""
    path = uri_to_path(str(loc.get("uri", "")))
    try:
        shown = str(Path(path).resolve().relative_to(root))
    except ValueError:
        shown = path
    line = loc.get("range", {}).get("start", {}).get("line")
    return f"{shown}:{int(line) + 1}" if isinstance(line, int) else shown


def _lsp_lookup(  # pragma: no cover - requires a real server on PATH
    manager: LspManager,
    resolved: Path,
    root: Path,
    language: str,
    name: str,
    line: int,
    action: str,
) -> str | None:
    """Answer via LSP, or ``None`` to fall back (no server, symbol not on the line, or an error)."""
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows = text.split("\n")
    if not (1 <= line <= len(rows)):
        return None
    col = _column_of(rows[line - 1], name)
    if col is None:
        return None
    client = manager.client_for(str(root), language)
    if client is None:
        return None
    uri = resolved.resolve().as_uri()
    try:
        client.sync(uri, language, text)
        if action == "hover":
            hover = _hover_text(client.hover(uri, line - 1, col))
            return f"hover for {name!r} (language server):\n{hover}" if hover else None
        locs = (
            client.definition(uri, line - 1, col)
            if action == "definition"
            else client.references(uri, line - 1, col)
        )
    except (LspError, OSError):
        return None
    if not locs:
        # Empty could mean "genuinely none" OR "server still indexing" — fall back to the textual
        # search rather than assert a definitive negative (honors the always-fall-back contract).
        return None
    shown = [_loc_to_line(loc, root) for loc in locs[:_MAX_LOCS]]
    more = f"\n…({len(locs) - len(shown)} more)" if len(locs) > len(shown) else ""
    return f"{action} of {name!r} (language server):\n" + "\n".join(shown) + more


def _fallback(action: str, name: str, broker: AccessBroker, grep: Callable[..., str]) -> str:
    """Textual answer via grep — references = every use; definition = definition-shaped lines."""
    esc = re.escape(name)
    if action == "references":
        pattern = rf"\b{esc}\b"
    else:  # definition — common definition forms across languages
        keywords = "def|class|func|function|fn|type|struct|interface|trait|impl|enum|const|let|var"
        pattern = rf"(?:{keywords})\s+{esc}\b|\b{esc}\s*[:=]"
    return _FALLBACK_NOTE + grep(pattern, broker, ".", None, False, "content")


def symbol(
    action: str,
    name: str,
    path: str,
    broker: AccessBroker,
    *,
    line: int = 0,
    manager: LspManager | None = None,
    grep: Callable[..., str] | None = None,
) -> str:
    """Find the ``definition`` or ``references`` of ``name`` (LSP where possible, else grep)."""
    if action not in _ACTIONS:
        return "action must be 'definition' or 'references'."
    if not name or not path:
        return "Tell me the symbol `name` and the `path` where you saw it."
    try:
        resolved = broker.ensure(path, write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not resolved.is_file():
        return f"There's no file at {resolved}."
    language = _language_for(path)
    if manager is not None and language and line:
        root = resolved.resolve().parent  # rough workspace root; the server widens it on initialize
        answer = _lsp_lookup(manager, resolved, root, language, name, line, action)
        if answer is not None:
            return answer
    from autobot.tools.code.search import grep as _grep

    _log.info("symbol action=%s name=%r fallback", action, name)
    return _fallback(action, name, broker, grep or _grep)


def register_symbol_tool(registry: ToolRegistry, broker: AccessBroker) -> LspManager:
    """Register the read-only ``symbol`` tool; return the shared, atexit-cleaned LSP manager."""
    import atexit

    manager = LspManager()
    atexit.register(manager.shutdown_all)

    def _handler(action: str = "definition", name: str = "", path: str = "", line: int = 0) -> str:
        try:
            line_no = int(line or 0)
        except (TypeError, ValueError):
            line_no = 0
        return symbol(action, name, path, broker, line=line_no, manager=manager)

    registry.register(
        ToolSpec(
            name="symbol",
            description=(
                "Navigate code by symbol. `action`: 'definition' (where it's defined), "
                "'references' (where it's used), or 'hover' (its type/signature). Pass the symbol "
                "`name`, the `path` you saw it in, and the 1-based `line`. Uses a language server "
                "for precise, scope-aware results when one is installed; otherwise falls back to a "
                "textual search. Prefer this over grep for navigating code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["definition", "references", "hover"],
                        "description": "What to find.",
                    },
                    "name": {"type": "string", "description": "The symbol name."},
                    "path": {"type": "string", "description": "File where you saw the symbol."},
                    "line": {"type": "integer", "description": "1-based line where it appears."},
                },
                "required": ["action", "name", "path"],
            },
            handler=_handler,
            risk=Risk.READ_ONLY,
            ack="Looking up the symbol.",
        )
    )
    return manager
