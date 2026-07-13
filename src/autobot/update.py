"""Version-check and self-replace cores behind ``jack update`` (pure; thin I/O elsewhere).

Everything here is deterministic and injectable — the network fetch and the clock are
passed in — so the update logic unit-tests without touching GitHub or the running
binary. The thin network/extract wrappers live at the bottom (``pragma: no cover``).
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

_CHECK_INTERVAL_S = 86400.0  # notice at most once/day
_OS = {"darwin": "macos", "linux": "linux", "windows": "windows"}
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}


def cache_path() -> Path:
    """Where the throttled update check caches its last result."""
    return Path.home() / ".autobot" / "update-check.json"


def asset_name(version: str, system: str, machine: str) -> str:
    """Release asset filename for a platform, e.g. ``jack-0.6.3-macos-arm64.tar.gz``.

    Raises:
        ValueError: if the OS/arch isn't one we publish.
    """
    try:
        os_key = _OS[system.lower()]
        arch = _ARCH[machine.lower()]
    except KeyError as exc:  # unknown platform — caller surfaces a clear message
        raise ValueError(f"unsupported platform: {system}/{machine}") from exc
    ext = "zip" if os_key == "windows" else "tar.gz"
    return f"jack-{version}-{os_key}-{arch}.{ext}"


def version_gt(a: str, b: str) -> bool:
    """True if semver ``a`` is strictly greater than ``b`` (numeric field compare)."""

    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    return parts(a) > parts(b)


def sha256_of(path: Path) -> str:
    """Hex SHA-256 of a file, streamed so a large binary doesn't load into memory."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_for_update(
    current: str,
    now: float,
    cache_path: Path,
    fetch_latest: Callable[[], str | None],
    interval_s: float = _CHECK_INTERVAL_S,
) -> str | None:
    """Return the latest version if newer than ``current``, else ``None`` — throttled.

    Reads/writes ``cache_path`` (``{"last_check", "latest"}``). Within ``interval_s`` of
    the last check it answers from cache and never calls ``fetch_latest``; otherwise it
    fetches, caches the result, and compares. Never raises — a bad cache, a failed fetch,
    or an unparseable version yields ``None``.
    """
    cached = _read_cache(cache_path)
    if cached is not None and (now - cached[0]) < interval_s:
        latest = cached[1]
    else:
        try:
            fetched = fetch_latest()
        except Exception:  # network is best-effort — never break the CLI over it
            return None
        if not fetched:
            return None
        latest = fetched
        _write_cache(cache_path, now, latest)
    try:
        return latest if version_gt(latest, current) else None
    except ValueError:  # non-numeric version string — ignore
        return None


def self_replace(new_binary: Path, target: Path) -> None:
    """Atomically replace ``target`` with ``new_binary`` (already downloaded + verified).

    POSIX: make it executable, then ``Path.replace`` (atomic same-filesystem rename).
    Windows: a running ``.exe`` can be *renamed* but not overwritten, so move the live
    binary aside first, then move the new one into place; the ``.old`` file is cleaned
    up on the next update.
    """
    new_binary, target = Path(new_binary), Path(target)
    mode = new_binary.stat().st_mode
    new_binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform == "win32":  # pragma: no cover - exercised on the Windows CI smoke
        old = target.with_name(target.name + ".old")
        if old.exists():
            old.unlink()
        target.replace(old)
        new_binary.replace(target)
    else:
        new_binary.replace(target)


def _read_cache(path: Path) -> tuple[float, str] | None:
    try:
        data = json.loads(path.read_text())
        return float(data["last_check"]), str(data["latest"])
    except Exception:  # missing/corrupt cache → treat as no cache
        return None


def _write_cache(path: Path, now: float, latest: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_check": now, "latest": latest}))
    except OSError:  # caching is best-effort
        pass
