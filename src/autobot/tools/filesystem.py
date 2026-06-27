"""Filesystem tools — active-folder-aware, scoped by the access broker.

Each operation resolves its paths through the central :class:`~autobot.tools.access.AccessBroker`,
which applies the active folder (cwd) for relative paths and the grant list for absolute
ones. Risk levels drive the permission gate: reading and listing are ``READ_ONLY``
(no confirmation), creating and moving are reversible (``WRITE``), deleting is not
(``DESTRUCTIVE``) and therefore prompts for confirmation.

Acting handlers report the file's **absolute path** so the assistant can tell the
user exactly where the file is, and ``read_file``/``list_files`` let it actually
inspect the active folder — so it can confirm a file exists (or is really gone after a
delete) instead of guessing.

Handlers return human-readable strings and never raise out of the method; errors
from the broker (``AccessDeniedError``, ``PermissionError``) are caught and returned
as strings so a denied path can't crash the turn loop.
"""

from __future__ import annotations

import shutil

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.registry import ToolRegistry, ToolSpec

_PATH_PROP = {
    "type": "string",
    "description": "Path in the active folder (relative), or an absolute path elsewhere.",
}
_MAX_READ_BYTES = 20_000
_MAX_LIST_ENTRIES = 200


class FileTools:
    """Filesystem operations scoped by the access policy + active folder (cwd)."""

    def __init__(self, broker: AccessBroker) -> None:
        self._broker = broker

    def create_file(self, path: str, content: str = "") -> str:
        """Create (or overwrite) a file in the active folder (or a granted path)."""
        try:
            target = self._broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"created {target.name} ({len(content)} bytes) at {target}"

    def read_file(self, path: str) -> str:
        """Read a file's contents from the active folder (or a granted path)."""
        try:
            target = self._broker.ensure(path, write=False)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"that's a folder, not a file: {path}"
        data = target.read_text(encoding="utf-8", errors="replace")
        if len(data) > _MAX_READ_BYTES:
            data = data[:_MAX_READ_BYTES] + "\n…(truncated)"
        return f"{target.name} (at {target}):\n{data}"

    def list_files(self, subdir: str = "") -> str:
        """List files in the active folder (or a sub-folder / granted path)."""
        try:
            base = self._broker.ensure(subdir or ".", write=False)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not base.exists():
            return f"not found: {subdir or '.'}"
        if base.is_file():
            return f"{base.name} exists ({base.stat().st_size} bytes) at {base}"
        files = sorted(p for p in base.rglob("*") if p.is_file())
        if not files:
            return f"no files in {base}"
        shown = files[:_MAX_LIST_ENTRIES]
        lines = [f"{p.relative_to(base)} ({p.stat().st_size} bytes)" for p in shown]
        more = "" if len(files) <= _MAX_LIST_ENTRIES else f"\n…and {len(files) - len(shown)} more"
        return f"{len(files)} file(s) in {base}:\n" + "\n".join(lines) + more

    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file (within the active folder or granted paths)."""
        try:
            src = self._broker.ensure(source, write=True)
            dst = self._broker.ensure(destination, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not src.exists():
            return f"source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src.name} -> {dst.name} (now at {dst})"

    def delete_file(self, path: str) -> str:
        """Delete a file in the active folder (or a granted path); irreversible."""
        try:
            target = self._broker.ensure(path, write=True)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"refusing to delete a folder: {path}"
        target.unlink()
        gone = "confirmed gone" if not target.exists() else "but it still appears to exist"
        return f"deleted {target.name} ({gone})"

    def specs(self) -> list[ToolSpec]:
        """Tool specs with risk levels set; descriptions reflect the active folder."""
        return [
            ToolSpec(
                name="create_file",
                description=(
                    "Create a file in the user's ACTIVE folder (the current working "
                    "directory). Pass a relative name (e.g. 'notes.txt') to put it in the "
                    "active folder, or an absolute path to put it elsewhere (Jack asks to "
                    "grant a new folder on first use). Returns the file's full path."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": _PATH_PROP,
                        "content": {"type": "string", "description": "File contents."},
                    },
                    "required": ["path"],
                },
                handler=self.create_file,
                risk=Risk.WRITE,
                ack="Creating that file.",
            ),
            ToolSpec(
                name="read_file",
                description=(
                    "Read a file's contents from the active folder (relative name) or an "
                    "absolute path. Use it to check what a file contains or confirm it exists."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                handler=self.read_file,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="list_files",
                description=(
                    "List files in the active folder (or a sub-folder / absolute path). Use "
                    "it to find a file or confirm one exists, e.g. after creating or deleting."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "subdir": {
                            "type": "string",
                            "description": "Optional sub-folder; omit for the active folder.",
                        }
                    },
                },
                handler=self.list_files,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="move_file",
                description="Move or rename a file (active folder, or granted absolute paths).",
                parameters={
                    "type": "object",
                    "properties": {"source": _PATH_PROP, "destination": _PATH_PROP},
                    "required": ["source", "destination"],
                },
                handler=self.move_file,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="delete_file",
                description=(
                    "Delete a file in the active folder (or a granted path). Cannot be undone."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                handler=self.delete_file,
                risk=Risk.DESTRUCTIVE,
            ),
        ]


def register_filesystem_tools(registry: ToolRegistry, broker: AccessBroker) -> FileTools:
    """Register the filesystem tools (scoped by the access policy + active folder).

    Args:
        registry: The tool registry to register into.
        broker: The access broker that provides cwd-relative path resolution.

    Returns:
        The :class:`FileTools` instance (holding the broker), for reference.
    """
    tools = FileTools(broker)
    for spec in tools.specs():
        registry.register(spec)
    return tools
