"""Bump (or verify) the project version across every manifest.

The version lives in three files — ``pyproject.toml`` (engine), ``src-tauri/
Cargo.toml`` and ``src-tauri/tauri.conf.json`` (orb app) — and they must always
agree, because the release workflow checks them against the pushed git tag.

Usage::

    python scripts/bump_version.py 0.2.0     # rewrite all three to 0.2.0
    python scripts/bump_version.py --check 0.2.0   # verify all three == 0.2.0

We deliberately do NOT commit or tag for you — the script prints the git commands
to run, so committing stays an explicit step.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# rel-path -> (pattern matching the current version line, replacement template).
_FILES: dict[str, tuple[re.Pattern[str], str]] = {
    "pyproject.toml": (re.compile(r'(?m)^version = "[^"]+"'), 'version = "{v}"'),
    "ui/orb-shell/src-tauri/Cargo.toml": (re.compile(r'(?m)^version = "[^"]+"'), 'version = "{v}"'),
    "ui/orb-shell/src-tauri/tauri.conf.json": (
        re.compile(r'"version": "[^"]+"'),
        '"version": "{v}"',
    ),
}


def set_version(text: str, pattern: re.Pattern[str], template: str, version: str) -> str:
    """Return ``text`` with the first version match replaced by ``version``.

    Raises:
        ValueError: if the version line isn't found exactly once.
    """
    new, count = pattern.subn(template.format(v=version), text, count=1)
    if count != 1:
        raise ValueError("version line not found")
    return new


def current_version(text: str, pattern: re.Pattern[str]) -> str | None:
    """Extract the version string from a manifest's matched line, or ``None``."""
    match = pattern.search(text)
    if match is None:
        return None
    found = re.search(r"\d+\.\d+\.\d+", match.group(0))
    return found.group(0) if found else None


def _bump(version: str, root: Path = _ROOT) -> None:
    for rel, (pattern, template) in _FILES.items():
        path = root / rel
        path.write_text(set_version(path.read_text(), pattern, template, version))
        print(f"updated {rel} -> {version}")
    print(
        f"\nNext (commit is up to you):\n"
        f"  git add -A && git commit -m 'release v{version}'\n"
        f"  git tag v{version} && git push origin main --tags"
    )


def _check(version: str, root: Path = _ROOT) -> int:
    """Return 0 if every manifest is at ``version``, else 1 (printing mismatches)."""
    ok = True
    for rel, (pattern, _template) in _FILES.items():
        found = current_version((root / rel).read_text(), pattern)
        if found != version:
            ok = False
            print(f"MISMATCH {rel}: {found!r} != {version!r}")
    if ok:
        print(f"all manifests at {version}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    """CLI entry point; see the module docstring for usage."""
    if len(argv) == 3 and argv[1] == "--check" and _SEMVER.match(argv[2]):
        return _check(argv[2])
    if len(argv) == 2 and _SEMVER.match(argv[1]):
        _bump(argv[1])
        return 0
    print("usage: bump_version.py X.Y.Z  |  bump_version.py --check X.Y.Z")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
