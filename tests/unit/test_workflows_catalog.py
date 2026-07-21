"""The active-workflows accessor and the prompt catalog block."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from autobot.llm.ollama_llm import workflows_catalog_block
from autobot.workflows.registry import WorkflowDir, WorkflowRegistry
from autobot.workflows.state import active_workflows, set_active_workflows


@pytest.fixture(autouse=True)
def _reset_active() -> Generator[None, None, None]:
    set_active_workflows(None)
    yield
    set_active_workflows(None)


def test_block_empty_when_no_active_registry() -> None:
    assert active_workflows() is None
    assert workflows_catalog_block() == ""


def test_block_returns_catalog_when_active(tmp_path: Path) -> None:
    d = tmp_path / "deploy-app"
    d.mkdir()
    (d / "WORKFLOW.md").write_text(
        "---\nname: deploy-app\ndescription: Deploy application to production.\n---\n\n"
        "```yaml\nsteps: []\n```\n",
        encoding="utf-8",
    )
    set_active_workflows(WorkflowRegistry([WorkflowDir(tmp_path, "user", 20)]))
    block = workflows_catalog_block()
    assert "deploy-app" in block
    assert "run_workflow(" in block
