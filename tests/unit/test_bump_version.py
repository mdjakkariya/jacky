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
    pat, tpl = bump._CLI_FILES["pyproject.toml"]
    out = bump.set_version('name = "x"\nversion = "0.1.0"\n', pat, tpl, "0.2.0")
    assert 'version = "0.2.0"' in out
    assert 'name = "x"' in out  # only the version line changed


def test_set_version_in_tauri_json() -> None:
    pat, tpl = bump._ORB_FILES["ui/orb-shell/src-tauri/tauri.conf.json"]
    out = bump.set_version('{\n  "version": "0.1.0"\n}', pat, tpl, "1.2.3")
    assert '"version": "1.2.3"' in out


def test_set_version_in_uv_lock_targets_the_autobot_package() -> None:
    pat, tpl = bump._CLI_FILES["uv.lock"]
    src = '[[package]]\nname = "autobot"\nversion = "0.1.0"\nsource = { editable = "." }\n'
    out = bump.set_version(src, pat, tpl, "0.2.0")
    assert 'name = "autobot"\nversion = "0.2.0"' in out
    ref = '{ name = "autobot", extras = ["wake"] }\nname = "autobot"\nversion = "0.1.0"\n'
    out2 = bump.set_version(ref, pat, tpl, "9.9.9")
    assert '{ name = "autobot", extras = ["wake"] }' in out2
    assert 'version = "9.9.9"' in out2


def test_set_version_raises_when_absent() -> None:
    pat, tpl = bump._CLI_FILES["pyproject.toml"]
    try:
        bump.set_version("no version here", pat, tpl, "0.2.0")
    except ValueError:
        return
    raise AssertionError("expected ValueError when no version line is present")


def test_current_version_extracts_semver() -> None:
    pat, _tpl = bump._CLI_FILES["pyproject.toml"]
    assert bump.current_version('version = "3.4.5"\n', pat) == "3.4.5"
    assert bump.current_version("nope", pat) is None


def test_each_track_is_internally_in_sync() -> None:
    # Within a track every manifest must agree; the two tracks may now differ.
    root = _PATH.parent.parent
    for files in (bump._CLI_FILES, bump._ORB_FILES):
        versions = {
            rel: bump.current_version((root / rel).read_text(), pat)
            for rel, (pat, _tpl) in files.items()
        }
        assert len(set(versions.values())) == 1, versions


def _seed(root: Path) -> None:
    """Write minimal manifest fixtures (all at 0.1.0) under a tmp root."""
    (root / "src" / "autobot").mkdir(parents=True)
    (root / "ui" / "orb-shell" / "src-tauri").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (root / "src" / "autobot" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (root / "uv.lock").write_text('[[package]]\nname = "autobot"\nversion = "0.1.0"\n')
    st = root / "ui" / "orb-shell" / "src-tauri"
    (st / "Cargo.toml").write_text('[package]\nversion = "0.1.0"\n')
    (st / "tauri.conf.json").write_text('{\n  "version": "0.1.0"\n}\n')
    (st / "Cargo.lock").write_text('[[package]]\nname = "jack-orb"\nversion = "0.1.0"\n')


def test_bump_cli_leaves_orb_untouched(tmp_path: Path) -> None:
    _seed(tmp_path)
    bump._bump(bump._CLI_FILES, "0.7.0", "cli", root=tmp_path)
    assert "0.7.0" in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "0.7.0"' in (tmp_path / "src" / "autobot" / "__init__.py").read_text()
    # orb group must be unchanged
    assert "0.1.0" in (tmp_path / "ui/orb-shell/src-tauri/tauri.conf.json").read_text()


def test_bump_orb_leaves_cli_untouched(tmp_path: Path) -> None:
    _seed(tmp_path)
    bump._bump(bump._ORB_FILES, "0.3.0", "orb", root=tmp_path)
    assert '"version": "0.3.0"' in (tmp_path / "ui/orb-shell/src-tauri/tauri.conf.json").read_text()
    assert "0.1.0" in (tmp_path / "pyproject.toml").read_text()  # cli untouched


def test_check_is_per_track(tmp_path: Path) -> None:
    _seed(tmp_path)
    bump._bump(bump._CLI_FILES, "0.7.0", "cli", root=tmp_path)
    assert bump._check(bump._CLI_FILES, "0.7.0", root=tmp_path) == 0
    assert bump._check(bump._CLI_FILES, "0.1.0", root=tmp_path) == 1  # cli moved
    assert bump._check(bump._ORB_FILES, "0.1.0", root=tmp_path) == 0  # orb still 0.1.0


def test_main_rejects_unknown_track_and_bad_semver() -> None:
    assert bump.main(["bump_version.py", "nope", "0.1.0"]) == 2
    assert bump.main(["bump_version.py", "cli", "not-semver"]) == 2
    assert bump.main(["bump_version.py", "--check", "orb", "1.2"]) == 2
