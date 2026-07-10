"""A global list of workspace folders the user has trusted for the coder to act in.

Trusting a folder is an explicit, remembered decision (like VS Code / Claude Code workspace
trust): only inside a trusted folder will the coder read, write, or run commands. The store
is a small JSON file (``~/.autobot/trust.json``); paths are stored resolved. The path is
injectable so this unit-tests against a temp file.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_TRUST_FILE = Path("~/.autobot/trust.json").expanduser()


def _load(path: Path) -> list[str]:
    """The stored trusted-folder list, or ``[]`` if missing/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    trusted = data.get("trusted") if isinstance(data, dict) else None
    return [str(p) for p in trusted] if isinstance(trusted, list) else []


def is_trusted(folder: str | Path, *, path: Path = DEFAULT_TRUST_FILE) -> bool:
    """True if ``folder`` (resolved) has been trusted."""
    return str(Path(folder).expanduser().resolve()) in _load(path)


def add_trust(folder: str | Path, *, path: Path = DEFAULT_TRUST_FILE) -> None:
    """Record ``folder`` (resolved) as trusted (idempotent)."""
    target = str(Path(folder).expanduser().resolve())
    trusted = _load(path)
    if target not in trusted:
        trusted.append(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trusted": trusted}, indent=2), encoding="utf-8")


def trusted_folders(*, path: Path = DEFAULT_TRUST_FILE) -> list[str]:
    """All trusted folders (resolved paths)."""
    return _load(path)
