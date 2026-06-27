"""Tests for the set_working_directory tool."""

from __future__ import annotations

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.workspace import set_working_directory


class _Yes:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return "write"


class _No:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return False

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return ""


def test_set_working_directory_grants_and_sets(tmp_path: object) -> None:
    from pathlib import Path

    tmp = Path(str(tmp_path))
    ws = tmp / "workspace"
    proj = tmp / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp / "access.json", ws)
    out = set_working_directory(str(proj), AccessBroker(pol, _Yes()), pol)
    assert pol.cwd == proj.resolve()
    assert proj.name in out


def test_set_working_directory_declined_leaves_cwd(tmp_path: object) -> None:
    from pathlib import Path

    tmp = Path(str(tmp_path))
    ws = tmp / "workspace"
    proj = tmp / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp / "access.json", ws)
    out = set_working_directory(str(proj), AccessBroker(pol, _No()), pol)
    assert pol.cwd == ws.resolve()  # unchanged
    assert "access" in out.lower() or "couldn't" in out.lower()
