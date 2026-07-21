from __future__ import annotations

from pathlib import Path

import pytest

from autobot.workflows.spec import WorkflowError, parse_workflow

WF = """---
name: release-notes
description: Draft release notes from commits since a tag. Use when preparing a changelog.
inputs:
  - name: since_tag
    required: true
---

```yaml
steps:
  - tool: run_command
    args: { command: "git log {since_tag}..HEAD --oneline" }
    save_as: commits
  - tool: write_file
    args: { path: "NOTES.md", content: "{commits}" }
    when: "{commits}"
```
"""


def test_parses_frontmatter_and_steps() -> None:
    wf = parse_workflow(WF, path=Path("/w/release-notes/WORKFLOW.md"))
    assert wf.name == "release-notes"
    assert wf.required_inputs == ("since_tag",)
    assert len(wf.steps) == 2
    assert wf.steps[0].tool == "run_command" and wf.steps[0].save_as == "commits"
    assert wf.steps[1].when == "{commits}"


def test_missing_steps_block_raises() -> None:
    with pytest.raises(WorkflowError):
        parse_workflow("---\nname: x\ndescription: y\n---\n\nno steps here", path=Path("x"))


def test_bad_name_raises() -> None:
    with pytest.raises(WorkflowError):
        parse_workflow(
            "---\nname: Bad Name\ndescription: y\n---\n\n```yaml\nsteps: []\n```", path=Path("x")
        )


def test_step_without_tool_raises() -> None:
    bad = "---\nname: w\ndescription: d\n---\n\n```yaml\nsteps:\n  - args: {}\n```"
    with pytest.raises(WorkflowError):
        parse_workflow(bad, path=Path("x"))
