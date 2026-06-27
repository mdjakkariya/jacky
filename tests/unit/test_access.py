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


def test_cwd_defaults_to_workspace(tmp_path: Path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    assert pol.cwd == ws.resolve()


def test_resolve_joins_relative_onto_cwd(tmp_path: Path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    assert pol.resolve("notes.txt") == (ws.resolve() / "notes.txt")
    # Absolute paths are returned resolved, not joined.
    abs_p = tmp_path / "elsewhere" / "a.txt"
    assert pol.resolve(str(abs_p)) == abs_p.resolve()


def test_set_cwd_requires_a_write_grant_then_persists(tmp_path: Path) -> None:
    from autobot.tools.access import AccessPolicy, NeedsAccessError

    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    store = tmp_path / "access.json"
    pol = AccessPolicy(store, ws)
    # Not granted yet -> refuses with NeedsAccessError (caller can prompt).
    try:
        pol.set_cwd(proj)
        raise AssertionError("expected NeedsAccessError")
    except NeedsAccessError:
        pass
    pol.grant(proj, write=True)
    assert pol.set_cwd(proj) == proj.resolve()
    # Persisted: a fresh policy over the same store loads the cwd back.
    assert AccessPolicy(store, ws).cwd == proj.resolve()


def test_load_falls_back_when_saved_cwd_is_invalid(tmp_path: Path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    store = tmp_path / "access.json"
    store.write_text('{"cwd": "/nonexistent/gone", "grants": []}', encoding="utf-8")
    pol = AccessPolicy(store, ws)
    assert pol.cwd == ws.resolve()  # invalid saved cwd -> default workspace


def test_load_falls_back_when_saved_cwd_not_granted(tmp_path: Path) -> None:
    from autobot.tools.access import AccessPolicy

    ws = tmp_path / "workspace"
    other = tmp_path / "other"
    other.mkdir()  # exists, but never granted
    store = tmp_path / "access.json"
    store.write_text(f'{{"cwd": "{other}", "grants": []}}', encoding="utf-8")
    pol = AccessPolicy(store, ws)
    assert pol.cwd == ws.resolve()  # exists but ungranted -> default workspace


def test_set_cwd_refuses_denylisted_path(tmp_path: Path) -> None:
    from autobot.tools.access import AccessDeniedError, AccessPolicy

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    try:
        pol.set_cwd(tmp_path / ".ssh")
        raise AssertionError("expected AccessDeniedError")
    except AccessDeniedError:
        pass


def test_broker_ensure_resolves_relative_against_cwd(tmp_path: Path) -> None:
    from autobot.tools.access import AccessBroker, AccessPolicy

    class _Yes:
        def confirm(self, prompt: str, kind: str = "danger") -> bool:
            return True

        def choose(
            self,
            prompt: str,
            options: list[dict[str, str]],
            kind: str = "read",
            default: str = "read",
        ) -> str:
            return "write"

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    # A relative path is created inside the cwd (the workspace, always granted).
    resolved = broker.ensure("sub/a.txt", write=True)
    assert resolved == (ws.resolve() / "sub" / "a.txt")
