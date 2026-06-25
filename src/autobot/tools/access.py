"""Central filesystem access policy — one allowlist every file tool consults.

Instead of a per-tool jail, all file tools (read, copy, write, edit, …) route path
checks through a single :class:`AccessPolicy`. It is **deny-by-default**: a path is
permitted only if it resolves inside a *granted root* with a sufficient mode and is
not on the secret denylist. Folder grants are recursive and persist across launches
(``~/.autobot/access.json``); the workspace is always granted read-write.

This is the *scope* layer (which folders). The permission gate stays the *risk* layer
(how dangerous an op is). See ``docs/plans/file_access_model.md``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import ClassVar, Protocol

from autobot.logging_setup import get_logger

_log = get_logger("access")


class Mode(IntEnum):
    """A grant's authority. ``WRITE`` implies ``READ`` (higher is more)."""

    READ = 1
    WRITE = 2


class NeedsAccessError(Exception):
    """Raised when a path isn't covered by any grant (or needs a higher mode).

    Carries the *folder* to ask the user about and the mode required, so the caller
    can prompt for a grant and retry.
    """

    def __init__(self, folder: Path, mode: Mode) -> None:
        self.folder = folder
        self.mode = mode
        super().__init__(f"no {mode.name.lower()} access to {folder}")


class AccessDeniedError(Exception):
    """Raised when a path is on the secret denylist — never grantable by a tool."""


@dataclass(frozen=True, slots=True)
class Grant:
    """A granted root and the authority over it."""

    path: str
    mode: Mode


# Locations that stay off-limits even inside a granted root (defense in depth).
# Directory names matched anywhere in the path, plus exact-name files.
_DENY_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg", "Keychains", ".password-store"})
_DENY_FILE_NAMES = frozenset({".env"})
# Substrings in a filename that signal a secret/key.
_DENY_NAME_PARTS = ("id_rsa", "id_ed25519", "secret", "credentials", ".pem", ".key")


def _tilde(path: Path) -> str:
    """Shorten a home-relative path to ``~/…`` for friendlier prompts."""
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home) :] if home and s.startswith(home) else s


def _is_denied(resolved: Path) -> bool:
    """Whether a resolved path is on the secret denylist."""
    parts = set(resolved.parts)
    if parts & _DENY_DIR_NAMES:
        return True
    name = resolved.name.lower()
    if resolved.name in _DENY_FILE_NAMES:
        return True
    return any(p in name for p in _DENY_NAME_PARTS)


class AccessPolicy:
    """Deny-by-default allowlist of granted roots, shared by all file tools."""

    def __init__(self, store_path: str | Path, workspace_root: str | Path) -> None:
        self._store = Path(store_path).expanduser()
        # The workspace is always available read-write; not persisted.
        self._workspace = Path(workspace_root).expanduser().resolve()
        self._lock = threading.RLock()
        self._grants: dict[Path, Mode] = {}
        self._load()

    # --- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for g in data.get("grants", []):
            try:
                path = Path(str(g["path"])).expanduser().resolve()
                mode = Mode.WRITE if str(g.get("mode")) == "write" else Mode.READ
            except (KeyError, ValueError):
                continue
            self._grants[path] = mode

    def _save(self) -> None:
        payload = {
            "grants": [{"path": str(p), "mode": m.name.lower()} for p, m in self._grants.items()]
        }
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:  # never crash a turn over a save failure
            _log.warning("could not persist access grants: %s", exc)

    # --- queries / mutations ---------------------------------------------
    def _roots(self) -> dict[Path, Mode]:
        """All effective roots, including the always-on workspace (read-write)."""
        roots = dict(self._grants)
        roots[self._workspace] = Mode.WRITE
        return roots

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def check(self, path: str | Path, write: bool = False) -> Path:
        """Resolve ``path`` and confirm it's allowed for the requested op.

        Args:
            path: The file/dir to access (``~`` expanded; symlinks/``..`` resolved).
            write: True for a write/edit/delete; False for read.

        Returns:
            The resolved absolute path.

        Raises:
            AccessDeniedError: The path is on the secret denylist.
            NeedsAccessError: No granted root covers it at the required mode.
        """
        need = Mode.WRITE if write else Mode.READ
        resolved = Path(path).expanduser().resolve()
        if _is_denied(resolved):
            raise AccessDeniedError(f"{resolved} is a protected location")
        best: Mode | None = None
        for root, mode in self._roots().items():
            if self._within(resolved, root):
                best = mode if best is None else max(best, mode)
        if best is not None and best >= need:
            return resolved
        # Ask about the containing folder (a file's parent, or the dir itself).
        folder = resolved if resolved.is_dir() else resolved.parent
        raise NeedsAccessError(folder, need)

    def grant(self, path: str | Path, write: bool = False) -> Grant:
        """Grant access to a folder (recursive). Upgrades mode if already granted."""
        root = Path(path).expanduser().resolve()
        if _is_denied(root):
            raise AccessDeniedError(f"{root} is a protected location")
        mode = Mode.WRITE if write else Mode.READ
        with self._lock:
            existing = self._grants.get(root)
            self._grants[root] = mode if existing is None else max(existing, mode)
            self._save()
            granted = self._grants[root]
        _log.info("granted %s access to %s", granted.name.lower(), root)
        return Grant(str(root), granted)

    def revoke(self, path: str | Path) -> bool:
        """Remove a grant. Returns True if one was removed (the workspace can't be)."""
        root = Path(path).expanduser().resolve()
        with self._lock:
            removed = self._grants.pop(root, None) is not None
            if removed:
                self._save()
        if removed:
            _log.info("revoked access to %s", root)
        return removed

    def grants(self) -> list[Grant]:
        """The user-granted roots (newest first), excluding the implicit workspace."""
        with self._lock:
            return [Grant(str(p), m) for p, m in reversed(self._grants.items())]


# The process-wide policy, set by the composition root so the daemon's Settings
# endpoints (list/grant/revoke) can manage grants without threading it through build.
_active: AccessPolicy | None = None


def set_active_policy(policy: AccessPolicy | None) -> None:
    """Register the active policy for the daemon's access-management endpoints."""
    global _active
    _active = policy


def active_policy() -> AccessPolicy | None:
    """The active policy, or ``None`` (e.g. demo mode)."""
    return _active


class Confirming(Protocol):
    """The slice of the gate's confirmer the broker needs (confirm + choose)."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Ask a yes/no question; True to proceed."""
        ...

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        """Ask the user to pick an option's value; "" means cancel."""
        ...


class AccessBroker:
    """Resolves a path for a tool, asking the user to grant the folder on first use.

    Wraps :class:`AccessPolicy` with the gate's confirmer. On :class:`NeedsAccessError`:
    a read needs a *read grant* — asked with a calm card and a Read-only / Read & write
    choice (least-privilege default); a write needs a *write grant* — a plain confirm.
    On approval it grants and retries; otherwise it raises ``PermissionError`` (the tool
    turns that into a friendly message). A denylisted path raises ``AccessDeniedError``.
    """

    _LEVELS: ClassVar[list[dict[str, str]]] = [
        {"label": "Read only", "value": "read"},
        {"label": "Read & write", "value": "write"},
    ]

    def __init__(self, policy: AccessPolicy, confirmer: Confirming) -> None:
        self._policy = policy
        self._confirmer = confirmer

    def ensure(self, path: str | Path, write: bool = False) -> Path:
        """Return the resolved path if allowed, prompting for a grant if needed."""
        try:
            return self._policy.check(path, write)
        except NeedsAccessError as na:
            folder = _tilde(na.folder)  # shorter, friendlier than the absolute path
            # Reads naturally as Jack's own reply if the turn ends here (the loop's
            # anti-repeat guard surfaces this verbatim) — no model-only jargon.
            denied = (
                f"I don't have access to {folder}, so I couldn't do that. You can grant it "
                'in Settings → Folders & access (or next time choose "Read & write"), then '
                "ask me again."
            )
            if write:
                prompt = f"Let Jack create and edit files in {folder}?"
                if not self._confirmer.confirm(prompt, "write"):
                    raise PermissionError(denied) from na
                self._policy.grant(na.folder, write=True)
            else:
                prompt = f"Let Jack into the folder {folder}?"
                choice = self._confirmer.choose(prompt, self._LEVELS, "read", "read")
                if choice not in ("read", "write"):
                    raise PermissionError(denied) from na
                self._policy.grant(na.folder, write=(choice == "write"))
            return self._policy.check(path, write)
