"""Filesystem tools — the first genuinely-acting tools, sandboxed and gated.

Each operation resolves its paths through a :class:`~autobot.tools.sandbox.Sandbox`,
so nothing outside the workspace can be touched even if the model asks. Risk
levels drive the permission gate: creating and moving are reversible (``WRITE``),
deleting is not (``DESTRUCTIVE``) and therefore prompts for confirmation.

Handlers return human-readable strings and let exceptions propagate to the
registry, which converts them into failed ``ToolResult``s.
"""

from __future__ import annotations

import shutil

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.sandbox import Sandbox

_PATH_PROP = {"type": "string", "description": "Path relative to the workspace."}


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
        return f"created {rel} ({len(content)} bytes)"

    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file within the workspace."""
        src = self._sandbox.resolve(source)
        dst = self._sandbox.resolve(destination)
        if not src.exists():
            return f"source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        root = self._sandbox.root
        return f"moved {src.relative_to(root)} -> {dst.relative_to(root)}"

    def delete_file(self, path: str) -> str:
        """Delete a file in the workspace (irreversible)."""
        target = self._sandbox.resolve(path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"refusing to delete a directory: {path}"
        rel = target.relative_to(self._sandbox.root)
        target.unlink()
        return f"deleted {rel}"

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs for these operations, with risk levels set."""
        return [
            ToolSpec(
                name="create_file",
                description="Create or overwrite a file in the workspace.",
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
