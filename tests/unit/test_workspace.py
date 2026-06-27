"""Tests for the set_working_directory tool."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.registry import ToolRegistry
from autobot.tools.workspace import register_workspace_tools, set_working_directory


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


def test_set_working_directory_empty_path_returns_prompt(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    # Empty string
    out = set_working_directory("", broker, pol)
    assert "Tell me which folder" in out
    # Whitespace-only
    out2 = set_working_directory("   ", broker, pol)
    assert "Tell me which folder" in out2


def test_set_working_directory_file_path_returns_not_a_folder(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    # Create a file inside the workspace (always granted), then pass it as the path.
    f = ws / "afile.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("hi")
    out = set_working_directory(str(f), broker, pol)
    assert "not a folder" in out.lower() or "That's not a folder" in out


def test_register_workspace_tools_registers_set_working_directory(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    registry = ToolRegistry()
    register_workspace_tools(registry, broker, pol)
    spec = registry.get("set_working_directory")
    assert spec is not None
    assert spec.name == "set_working_directory"


def test_set_working_directory_set_cwd_failure_returns_message(tmp_path: Path) -> None:
    """The defensive except-Exception guard returns a friendly string on unexpected failure."""
    import unittest.mock as mock

    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    # Grant and point at a directory inside the workspace so ensure() succeeds,
    # then make set_cwd raise an unexpected RuntimeError to hit lines 34-36.
    subdir = ws / "project"
    subdir.mkdir(parents=True)
    pol.grant(subdir, write=True)
    with mock.patch.object(pol, "set_cwd", side_effect=RuntimeError("disk full")):
        out = set_working_directory(str(subdir), broker, pol)
    assert "couldn't" in out.lower() or "switch" in out.lower()
