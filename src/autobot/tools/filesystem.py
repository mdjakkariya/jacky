"""Filesystem tools — the first genuinely-acting tools, sandboxed and gated.

Each operation resolves its paths through a :class:`~autobot.tools.sandbox.Sandbox`,
so nothing outside the workspace can be touched even if the model asks. Risk
levels drive the permission gate: reading and listing are ``READ_ONLY`` (no
confirmation), creating and moving are reversible (``WRITE``), deleting is not
(``DESTRUCTIVE``) and therefore prompts for confirmation.

Acting handlers report the file's **absolute path** so the assistant can tell the
user exactly where the file is, and ``read_file``/``list_files`` let it actually
inspect the workspace — so it can confirm a file exists (or is really gone after a
delete) instead of guessing.

Handlers return human-readable strings and let exceptions propagate to the
registry, which converts them into failed ``ToolResult``s.
"""

from __future__ import annotations

import shutil

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.sandbox import Sandbox

_PATH_PROP = {"type": "string", "description": "Path relative to the workspace."}

_MAX_READ_BYTES = 20_000  # cap how much of a file we pull back into the prompt
_MAX_LIST_ENTRIES = 200  # cap a listing so a big tree can't flood the context


class FileTools:
    """Sandbox-bound filesystem operations exposed as tools."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def create_file(self, path: str, content: str = "") -> str:
        """Create (or overwrite) a file in the workspace with the given content."""
        target = self._sandbox.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = target.relative_to(self._sandbox.root)
        return f"created {rel} ({len(content)} bytes) at {target}"

    def read_file(self, path: str) -> str:
        """Read a file's contents from the workspace (truncated if very large)."""
        target = self._sandbox.resolve(path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"that's a folder, not a file: {path}"
        data = target.read_text(encoding="utf-8", errors="replace")
        rel = target.relative_to(self._sandbox.root)
        if len(data) > _MAX_READ_BYTES:
            data = data[:_MAX_READ_BYTES] + "\n…(truncated)"
        return f"{rel} (at {target}):\n{data}"

    def list_files(self, subdir: str = "") -> str:
        """List files in the workspace (or a sub-folder); use it to confirm a file."""
        base = self._sandbox.resolve(subdir) if subdir else self._sandbox.root
        if not base.exists():
            return f"not found: {subdir or '.'}"
        root = self._sandbox.root
        if base.is_file():
            return f"{base.relative_to(root)} exists ({base.stat().st_size} bytes) at {base}"
        files = sorted(p for p in base.rglob("*") if p.is_file())
        if not files:
            return f"no files in {base}"
        shown = files[:_MAX_LIST_ENTRIES]
        lines = [f"{p.relative_to(root)} ({p.stat().st_size} bytes)" for p in shown]
        more = "" if len(files) <= _MAX_LIST_ENTRIES else f"\n…and {len(files) - len(shown)} more"
        return f"{len(files)} file(s) in {base}:\n" + "\n".join(lines) + more

    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file within the workspace."""
        src = self._sandbox.resolve(source)
        dst = self._sandbox.resolve(destination)
        if not src.exists():
            return f"source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        root = self._sandbox.root
        return f"moved {src.relative_to(root)} -> {dst.relative_to(root)} (now at {dst})"

    def delete_file(self, path: str) -> str:
        """Delete a file in the workspace (irreversible); confirms it's really gone."""
        target = self._sandbox.resolve(path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"refusing to delete a folder: {path}"
        rel = target.relative_to(self._sandbox.root)
        target.unlink()
        # Verify removal so the assistant can confirm truthfully (not just assume).
        gone = "confirmed gone" if not target.exists() else "but it still appears to exist"
        return f"deleted {rel} ({gone})"

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs for these operations, with risk levels set."""
        return [
            ToolSpec(
                name="create_file",
                description=(
                    "Create a file in Jack's PRIVATE scratch workspace only (not the user's "
                    "own folders). For a file the user names a place for — their Documents, a "
                    "project folder, anywhere under their home — use write_file instead. "
                    "Returns the file's full path."
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
            ),
            ToolSpec(
                name="read_file",
                description=(
                    "Read a file's contents from the workspace. Use this to check what "
                    "a file contains, or to verify that a file exists before answering."
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
                    "List the files in the workspace (optionally a sub-folder). Use it "
                    "to find a file, report where files are, or confirm whether a file "
                    "still exists — for example after creating or deleting one."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "subdir": {
                            "type": "string",
                            "description": "Optional sub-folder; omit for the whole workspace.",
                        }
                    },
                },
                handler=self.list_files,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="move_file",
                description="Move or rename a file within the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "source": _PATH_PROP,
                        "destination": _PATH_PROP,
                    },
                    "required": ["source", "destination"],
                },
                handler=self.move_file,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="delete_file",
                description="Delete a file in the workspace. This cannot be undone.",
                parameters={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                handler=self.delete_file,
                risk=Risk.DESTRUCTIVE,
            ),
        ]


def register_filesystem_tools(registry: ToolRegistry, sandbox: Sandbox) -> FileTools:
    """Register the sandboxed filesystem tools into ``registry``.

    Returns:
        The :class:`FileTools` instance (holding the sandbox), for reference.
    """
    tools = FileTools(sandbox)
    for spec in tools.specs():
        registry.register(spec)
    return tools
