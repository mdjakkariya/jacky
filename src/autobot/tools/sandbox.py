"""Filesystem sandbox: confine all file operations to a single workspace root.

This is the hard boundary behind the permission gate. Even if the model is
tricked into requesting a path outside the workspace, :meth:`Sandbox.resolve`
refuses it — the gate's confirmation is the second line of defence, not the only
one. Relative paths are interpreted against the root; absolute paths are allowed
only if they already fall inside it.
"""

from __future__ import annotations

from pathlib import Path


class SandboxError(Exception):
    """Raised when a requested path escapes the sandbox root."""


class Sandbox:
    """A path-jail rooted at a single directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The absolute, resolved sandbox root."""
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """Resolve ``path`` to an absolute path guaranteed to be inside the root.

        Args:
            path: A relative path (joined onto the root) or an absolute path
                (which must already be within the root).

        Returns:
            The resolved absolute path.

        Raises:
            SandboxError: If the resolved path falls outside the sandbox root.
        """
        candidate = Path(path)
        combined = candidate if candidate.is_absolute() else self._root / candidate
        # ``resolve`` collapses ``..`` and symlinks, so this defeats traversal.
        resolved = combined.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise SandboxError(f"path {str(path)!r} resolves outside the sandbox ({self._root})")
        return resolved
