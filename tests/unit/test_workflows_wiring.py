"""The workflows wiring block used by app.build(): registry + tool + active pointer."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from autobot.llm.ollama_llm import workflows_catalog_block
from autobot.tools.registry import ToolRegistry
from autobot.workflows.registry import WorkflowRegistry, default_workflow_dirs
from autobot.workflows.state import active_workflows, set_active_workflows
from autobot.workflows.tool import register_workflow_tools


@pytest.fixture(autouse=True)
def _reset_active() -> Generator[None, None, None]:
    set_active_workflows(None)
    yield
    set_active_workflows(None)


def _wire(home: Path, project: Path) -> ToolRegistry:
    """Mirror of app.build()'s workflows block (workflows_enabled == True)."""
    workflows = WorkflowRegistry(default_workflow_dirs(home, project))
    set_active_workflows(workflows)
    registry = ToolRegistry()
    register_workflow_tools(registry, workflows)
    return registry


def test_wiring_registers_tool_and_active_registry(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    workflow = project / ".jack" / "workflows" / "deploy-app"
    workflow.mkdir(parents=True)
    (workflow / "WORKFLOW.md").write_text(
        "---\nname: deploy-app\ndescription: Deploy application to production.\n---\n\n"
        "```yaml\nsteps: []\n```\n",
        encoding="utf-8",
    )
    registry = _wire(home, project)
    assert registry.get("run_workflow") is not None
    assert active_workflows() is not None
    assert "deploy-app" in workflows_catalog_block()
