"""build() assembles a code-tool registry under the coder profile (no fileio name clash)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.config import Settings
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.tools import register_code_tools
from autobot.tools.registry import ToolRegistry


class _FakeConfirmer:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(self, prompt, options, kind="read", default="read"):  # type: ignore[no-untyped-def]
        return default


def test_code_tools_and_fileio_names_collide(tmp_path: Path) -> None:
    # This is WHY the coder profile needs its own registry: registering both raises.
    from autobot.tools.fileio import register_file_io_tools

    reg = ToolRegistry()
    pol = AccessPolicy(store_path=tmp_path / "a.json", workspace_root=tmp_path / "ws")
    broker = AccessBroker(pol, _FakeConfirmer())
    register_file_io_tools(reg, broker)
    try:
        register_code_tools(reg, broker)
        raised = False
    except ValueError:
        raised = True
    assert raised  # write_file/edit_file names clash — coder must use a separate registry


def test_coder_registry_has_code_tools(tmp_path: Path) -> None:
    reg = ToolRegistry()
    pol = AccessPolicy(store_path=tmp_path / "a.json", workspace_root=tmp_path / "ws")
    broker = AccessBroker(pol, _FakeConfirmer())
    register_code_tools(reg, broker, allowlist=[], blocklist=[])
    names = {s.name for s in reg.specs()}
    assert {"read_file", "edit_file", "grep", "run_command", "repo_map"} <= names


def test_build_with_coder_profile_registers_code_tools(tmp_path: Path) -> None:
    # Point build() at a coder-profile Settings; it must assemble code tools, not fileio.
    import autobot.app as app

    settings = Settings(
        profile="coder",
        sandbox_dir=str(tmp_path / "ws"),
        access_store=str(tmp_path / "a.json"),
        audit_db=str(tmp_path / "a.db"),
        agent_session_dir=str(tmp_path / "sess"),
        memory_db=str(tmp_path / "m.db"),
        input_mode="ptt",
        session_log=False,
    )
    orch = app.build(settings)
    reg = orch._gate._registry  # white-box, consistent with existing test style
    names = {s.name for s in reg.specs()}
    assert "edit_file" in names and "run_command" in names
    assert "read_file_text" not in names  # the assistant's fileio tool is absent


def test_build_with_assistant_profile_registers_assistant_tools(tmp_path: Path) -> None:
    # The default (assistant) profile still assembles the fileio/assistant tool set and
    # NOT the code tools — the mirror of the coder branch, and it exercises the assistant
    # registration path (unchanged by this phase apart from being wrapped in `if not coder:`).
    import autobot.app as app

    settings = Settings(
        sandbox_dir=str(tmp_path / "ws"),
        access_store=str(tmp_path / "a.json"),
        audit_db=str(tmp_path / "a.db"),
        agent_session_dir=str(tmp_path / "sess"),
        memory_db=str(tmp_path / "m.db"),
        input_mode="ptt",
        session_log=False,
    )
    assert settings.profile == "assistant"
    orch = app.build(settings)
    names = {s.name for s in orch._gate._registry.specs()}
    assert "read_file_text" in names  # assistant fileio tool present
    assert "run_command" not in names and "repo_map" not in names  # code tools absent


def test_coder_build_jails_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The coder is jailed to the directory it was launched from (its cwd), NOT sandbox_dir —
    # so `jack "…"` edits the current project.
    import autobot.app as app
    from autobot.tools.access import active_policy

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)  # simulate running jack from a project directory
    settings = Settings(
        profile="coder",
        sandbox_dir=str(tmp_path / "ignored_sandbox"),
        access_store=str(tmp_path / "a.json"),
        audit_db=str(tmp_path / "a.db"),
        agent_session_dir=str(tmp_path / "sess"),
        memory_db=str(tmp_path / "m.db"),
        input_mode="ptt",
        session_log=False,
    )
    app.build(settings)
    pol = active_policy()
    assert pol is not None
    assert pol.cwd == project.resolve()  # jailed to cwd, not the sandbox
