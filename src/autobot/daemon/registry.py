"""A registry of running per-workspace coder daemons: which port serves which workspace.

Lets ``jack`` find (or spawn) the daemon for the directory you're in, so multiple workspaces
run side by side on their own ports instead of one shared daemon. The store is a JSON map
``~/.autobot/daemons.json`` of ``workspace path -> {port, pid}``. Paths are stored resolved;
the file path is injectable so this unit-tests against a temp file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_REGISTRY = Path("~/.autobot/daemons.json").expanduser()
_PORT_BASE = 8770
_PORT_SPAN = 130  # coder daemons live on ports 8770..8899 (off the assistant's 8765)


def _key(workspace: str | Path) -> str:
    return str(Path(workspace).expanduser().resolve())


def _read(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict[str, int]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic


def read(*, path: Path = DEFAULT_REGISTRY) -> dict[str, dict[str, int]]:
    """The full registry: ``{workspace_path: {"port": …, "pid": …}}``."""
    return _read(path)


def entry(workspace: str | Path, *, path: Path = DEFAULT_REGISTRY) -> dict[str, int] | None:
    """The recorded ``{port, pid}`` for ``workspace``, or None."""
    got = _read(path).get(_key(workspace))
    return got if isinstance(got, dict) else None


def record(workspace: str | Path, port: int, pid: int, *, path: Path = DEFAULT_REGISTRY) -> None:
    """Record (or replace) the daemon serving ``workspace``."""
    data = _read(path)
    data[_key(workspace)] = {"port": port, "pid": pid}
    _write(data, path)


def remove(workspace: str | Path, *, path: Path = DEFAULT_REGISTRY) -> None:
    """Drop ``workspace`` from the registry (best effort)."""
    data = _read(path)
    if data.pop(_key(workspace), None) is not None:
        _write(data, path)


def port_for(workspace: str | Path, taken: set[int]) -> int:
    """A deterministic port for ``workspace``: hashed into 8770..8899, then next free.

    Hashing keeps a workspace on the same port across runs (stable, predictable); ``taken``
    (ports already assigned to other workspaces) is skipped so distinct workspaces don't
    collide. Falls back to the hashed port if the whole range is somehow taken.
    """
    digest = int(hashlib.sha1(_key(workspace).encode("utf-8")).hexdigest()[:8], 16)
    start = digest % _PORT_SPAN
    for i in range(_PORT_SPAN):
        candidate = _PORT_BASE + ((start + i) % _PORT_SPAN)
        if candidate not in taken:
            return candidate
    return _PORT_BASE + start
