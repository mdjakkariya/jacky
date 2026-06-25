"""Tests for the version-bump helpers (scripts/bump_version.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bump_version.py"
_spec = importlib.util.spec_from_file_location("bump_version", _PATH)
assert _spec and _spec.loader
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)


def test_set_version_in_pyproject() -> None:
    pat, tpl = bump._FILES["pyproject.toml"]
    out = bump.set_version('name = "x"\nversion = "0.1.0"\n', pat, tpl, "0.2.0")
    assert 'version = "0.2.0"' in out
    assert 'name = "x"' in out  # only the version line changed


def test_set_version_in_tauri_json() -> None:
    pat, tpl = bump._FILES["ui/orb-shell/src-tauri/tauri.conf.json"]
    out = bump.set_version('{\n  "version": "0.1.0"\n}', pat, tpl, "1.2.3")
    assert '"version": "1.2.3"' in out


def test_set_version_in_uv_lock_targets_the_autobot_package() -> None:
    pat, tpl = bump._FILES["uv.lock"]
    src = '[[package]]\nname = "autobot"\nversion = "0.1.0"\nsource = { editable = "." }\n'
    out = bump.set_version(src, pat, tpl, "0.2.0")
    assert 'name = "autobot"\nversion = "0.2.0"' in out
    # A dependency *reference* to autobot (same name, inline) must NOT be rewritten —
    # only the [[package]] block (name line immediately followed by version line).
    ref = '{ name = "autobot", extras = ["wake"] }\nname = "autobot"\nversion = "0.1.0"\n'
    out2 = bump.set_version(ref, pat, tpl, "9.9.9")
    assert '{ name = "autobot", extras = ["wake"] }' in out2
    assert 'version = "9.9.9"' in out2


def test_set_version_raises_when_absent() -> None:
    pat, tpl = bump._FILES["pyproject.toml"]
    try:
        bump.set_version("no version here", pat, tpl, "0.2.0")
    except ValueError:
        return
    raise AssertionError("expected ValueError when no version line is present")


def test_current_version_extracts_semver() -> None:
    pat, _tpl = bump._FILES["pyproject.toml"]
    assert bump.current_version('version = "3.4.5"\n', pat) == "3.4.5"
    assert bump.current_version("nope", pat) is None


def test_repo_manifests_are_in_sync() -> None:
    # The three real manifests must already agree (release gate relies on this).
    root = _PATH.parent.parent
    versions = {
        rel: bump.current_version((root / rel).read_text(), pat)
        for rel, (pat, _tpl) in bump._FILES.items()
    }
    assert len(set(versions.values())) == 1, versions
