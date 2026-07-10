"""An untrusted workspace grants no access; a trusted one is read-write."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.tools.access import AccessPolicy, NeedsAccessError


def test_trusted_workspace_is_writable(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws, workspace_trusted=True)
    # check() returns the resolved path without raising when covered read-write.
    assert pol.check(ws / "foo.py", write=True) == (ws / "foo.py").resolve()


def test_untrusted_workspace_denies_access(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws, workspace_trusted=False)
    with pytest.raises(NeedsAccessError):
        pol.check(ws / "foo.py", write=True)
    with pytest.raises(NeedsAccessError):
        pol.check(ws / "foo.py", write=False)  # even reads are denied until trusted
