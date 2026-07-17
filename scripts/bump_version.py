"""Bump (or verify) the version for one release track.

Jack releases on two independent tracks, each with its own version and tag:

- ``cli`` (the engine/CLI) — ``pyproject.toml``, ``src/autobot/__init__.py``, and the
  ``jacky`` entry in ``uv.lock`` (the PyPI distribution name); tagged ``vX.Y.Z``.
- ``orb`` (the macOS app) — ``src-tauri/Cargo.toml``, ``src-tauri/tauri.conf.json``, and
  the ``jack-orb`` entry in ``src-tauri/Cargo.lock``; tagged ``orb-vX.Y.Z``.

Within a track the manifests must agree (the release workflow ``--check``s the CLI
track against the pushed ``v*`` tag); the two tracks version independently. The lockfile
entries are pinned by name so a later ``cargo``/``uv`` run doesn't re-touch a lockfile
and force a stray follow-up commit.

Usage::

    python scripts/bump_version.py cli 0.7.0        # bump the CLI/engine manifests
    python scripts/bump_version.py orb 0.3.0        # bump the orb (src-tauri) manifests
    python scripts/bump_version.py --check cli 0.7.0  # verify the CLI track

We deliberately do NOT commit or tag for you — the script prints the git commands
to run, so committing stays an explicit step.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Two independent release tracks. The CLI *is* the frozen engine, so its version lives
# in the Python manifests; the orb app versions separately in its src-tauri manifests.
# rel-path -> (pattern matching the current version line, replacement template).
_CLI_FILES: dict[str, tuple[re.Pattern[str], str]] = {
    "pyproject.toml": (re.compile(r'(?m)^version = "[^"]+"'), 'version = "{v}"'),
    # The engine's runtime version constant. It compiles into the frozen `jack`
    # binary (importlib.metadata dist files are NOT bundled by PyInstaller), so this
    # is the single source `jack --version` reads — keep it in lockstep here.
    "src/autobot/__init__.py": (
        re.compile(r'(?m)^__version__ = "[^"]+"'),
        '__version__ = "{v}"',
    ),
    # The engine's own entry in uv's lockfile (the editable ``jacky`` distribution),
    # pinned by name so we never touch a dependency.
    "uv.lock": (
        re.compile(r'name = "jacky"\nversion = "[^"]+"'),
        'name = "jacky"\nversion = "{v}"',
    ),
}
_ORB_FILES: dict[str, tuple[re.Pattern[str], str]] = {
    "ui/orb-shell/src-tauri/Cargo.toml": (re.compile(r'(?m)^version = "[^"]+"'), 'version = "{v}"'),
    "ui/orb-shell/src-tauri/tauri.conf.json": (
        re.compile(r'"version": "[^"]+"'),
        '"version": "{v}"',
    ),
    # The orb crate's own entry in the lockfile — pinned by name so `cargo tauri build`
    # doesn't rewrite Cargo.lock after the release commit.
    "ui/orb-shell/src-tauri/Cargo.lock": (
        re.compile(r'name = "jack-orb"\nversion = "[^"]+"'),
        'name = "jack-orb"\nversion = "{v}"',
    ),
}
_TRACKS: dict[str, dict[str, tuple[re.Pattern[str], str]]] = {"cli": _CLI_FILES, "orb": _ORB_FILES}


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


def _bump(
    files: dict[str, tuple[re.Pattern[str], str]], version: str, track: str, root: Path = _ROOT
) -> None:
    for rel, (pattern, template) in files.items():
        path = root / rel
        # Read/write UTF-8 explicitly — the manifests contain non-ASCII (em-dashes,
        # arrows in comments), and the OS default encoding (cp1252 on Windows CI) can't
        # decode them, which crashed the Windows release binary build.
        new = set_version(path.read_text(encoding="utf-8"), pattern, template, version)
        path.write_text(new, encoding="utf-8")
        print(f"updated {rel} -> {version}")
    tag = f"v{version}" if track == "cli" else f"orb-v{version}"
    print(
        f"\nNext (the bump lands via a PR — main is protected):\n"
        f"  git switch -c release/{tag} && git add -A"
        f" && git commit -m 'chore(release): {tag}'\n"
        f"  git push -u origin release/{tag} && gh pr create --fill"
        f"   # merge once checks pass\n"
        f"  git switch main && git pull && git tag {tag} && git push origin {tag}"
    )


def _check(files: dict[str, tuple[re.Pattern[str], str]], version: str, root: Path = _ROOT) -> int:
    """Return 0 if every manifest in this track is at ``version``, else 1."""
    ok = True
    for rel, (pattern, _template) in files.items():
        found = current_version((root / rel).read_text(encoding="utf-8"), pattern)
        if found != version:
            ok = False
            print(f"MISMATCH {rel}: {found!r} != {version!r}")
    if ok:
        print(f"all manifests at {version}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    """CLI entry point; see the module docstring for usage."""
    if len(argv) == 4 and argv[1] == "--check" and argv[2] in _TRACKS and _SEMVER.match(argv[3]):
        return _check(_TRACKS[argv[2]], argv[3])
    if len(argv) == 3 and argv[1] in _TRACKS and _SEMVER.match(argv[2]):
        _bump(_TRACKS[argv[1]], argv[2], argv[1])
        return 0
    print("usage: bump_version.py <cli|orb> X.Y.Z  |  bump_version.py --check <cli|orb> X.Y.Z")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
