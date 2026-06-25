"""Tests for the central filesystem AccessPolicy (deny-by-default grants)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.tools.access import AccessDeniedError, AccessPolicy, Mode, NeedsAccessError


def _policy(tmp_path: Path) -> tuple[AccessPolicy, Path]:
    ws = tmp_path / "workspace"
    return AccessPolicy(store_path=tmp_path / "access.json", workspace_root=ws), ws


def test_deny_by_default_raises_needs_access(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    with pytest.raises(NeedsAccessError) as ei:
        pol.check(tmp_path / "proj" / "main.py")
    assert ei.value.folder == (tmp_path / "proj").resolve()  # the file's folder
    assert ei.value.mode == Mode.READ


def test_grant_read_allows_read_not_write(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    pol.grant(proj)  # read-only
    f = proj / "a.txt"
    assert pol.check(f) == f.resolve()  # read ok
    with pytest.raises(NeedsAccessError) as ei:
        pol.check(f, write=True)  # write needs an upgrade
    assert ei.value.mode == Mode.WRITE


def test_grant_write_allows_both(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    pol.grant(proj, write=True)
    f = proj / "a.txt"
    assert pol.check(f) == f.resolve()
    assert pol.check(f, write=True) == f.resolve()


def test_workspace_is_always_writable(tmp_path: Path) -> None:
    pol, ws = _policy(tmp_path)
    f = ws / "note.txt"
    assert pol.check(f, write=True) == f.resolve()


def test_denylist_blocks_even_inside_a_grant(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    (tmp_path / ".ssh").mkdir()
    pol.grant(tmp_path, write=True)  # grant the parent
    with pytest.raises(AccessDeniedError):
        pol.check(tmp_path / ".ssh" / "id_rsa")
    with pytest.raises(AccessDeniedError):
        pol.grant(tmp_path / ".ssh")  # can't grant a protected location


def test_traversal_cannot_escape_a_root(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "outside").mkdir()
    pol.grant(proj, write=True)
    with pytest.raises(NeedsAccessError):  # ../outside resolves out of proj
        pol.check(proj / ".." / "outside" / "x")


def test_grants_persist_across_instances(tmp_path: Path) -> None:
    store, ws = tmp_path / "access.json", tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    AccessPolicy(store, ws).grant(proj, write=True)
    reloaded = AccessPolicy(store, ws)
    assert reloaded.check(proj / "a", write=True) == (proj / "a").resolve()
    assert [g.path for g in reloaded.grants()] == [str(proj.resolve())]


def test_revoke(tmp_path: Path) -> None:
    pol, _ = _policy(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    pol.grant(proj)
    assert pol.revoke(proj) is True
    assert pol.revoke(proj) is False  # already gone
    with pytest.raises(NeedsAccessError):
        pol.check(proj / "a")
