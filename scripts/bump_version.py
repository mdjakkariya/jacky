"""Bump (or verify) the project version across every manifest.

The version lives in ``pyproject.toml`` (engine), ``src-tauri/Cargo.toml`` and
``src-tauri/tauri.conf.json`` (orb app), and they must always agree because the
release workflow checks them against the pushed git tag. We also rewrite the
``jack-orb`` entry in ``src-tauri/Cargo.lock`` and the ``autobot`` entry in
``uv.lock`` so a later ``cargo``/``uv`` run doesn't re-touch a lockfile and force a
stray, changelog-polluting follow-up commit (and a surprise diff on the next pull).

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
    # The engine's runtime version constant. It compiles into the frozen `jack`
    # binary (importlib.metadata dist files are NOT bundled by PyInstaller), so this
    # is the single source `jack --version` reads — keep it in lockstep here.
    "src/autobot/__init__.py": (
        re.compile(r'(?m)^__version__ = "[^"]+"'),
        '__version__ = "{v}"',
    ),
    "ui/orb-shell/src-tauri/Cargo.toml": (re.compile(r'(?m)^version = "[^"]+"'), 'version = "{v}"'),
    "ui/orb-shell/src-tauri/tauri.conf.json": (
        re.compile(r'"version": "[^"]+"'),
        '"version": "{v}"',
    ),
    # The orb crate's own entry in the lockfile — pinned by name so we never touch a
    # dependency's version. Keeping it in sync here means `cargo tauri build` won't
    # rewrite Cargo.lock after the release commit (which created the maintenance noise).
    "ui/orb-shell/src-tauri/Cargo.lock": (
        re.compile(r'name = "jack-orb"\nversion = "[^"]+"'),
        'name = "jack-orb"\nversion = "{v}"',
    ),
    # The engine's own entry in uv's lockfile (the editable ``autobot`` package),
    # pinned by name so we never touch a dependency. Without this, ``uv sync``/``uv
    # run`` rewrites uv.lock to match pyproject after a release — producing a stray
    # version diff locally on the next pull.
    "uv.lock": (
        re.compile(r'name = "autobot"\nversion = "[^"]+"'),
        'name = "autobot"\nversion = "{v}"',
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
        f"\nNext (the bump lands via a PR — main is protected):\n"
        f"  git switch -c release/v{version} && git add -A"
        f" && git commit -m 'chore(release): v{version}'\n"
        f"  git push -u origin release/v{version} && gh pr create --fill"
        f"   # merge once checks pass\n"
        f"  git switch main && git pull && git tag v{version} && git push origin v{version}"
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
