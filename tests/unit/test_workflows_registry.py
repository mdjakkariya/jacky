"""WorkflowRegistry discovery, precedence, freshness, catalog, and workflow loading."""

from __future__ import annotations

from pathlib import Path

from autobot.workflows.registry import WorkflowDir, WorkflowRegistry, default_workflow_dirs


def _write_workflow(root: Path, name: str, description: str, steps_yaml: str = "steps: []") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "WORKFLOW.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n```yaml\n{steps_yaml}\n```\n",
        encoding="utf-8",
    )
    return md


def test_discovers_workflow(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_workflow(user, "deploy-app", "Deploy application to production.")
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20)])
    names = [s.name for s in reg.specs()]
    assert names == ["deploy-app"]


def test_catalog_empty_when_no_workflows(tmp_path: Path) -> None:
    reg = WorkflowRegistry([WorkflowDir(tmp_path / "nope", "user", 20)])
    assert reg.catalog() == ""


def test_catalog_lists_name_and_description(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_workflow(user, "deploy-app", "Deploy application to production.")
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20)])
    cat = reg.catalog()
    assert "deploy-app" in cat
    assert "Deploy application to production." in cat
    assert "run_workflow(" in cat  # tells the model how to activate


def test_project_overrides_user(tmp_path: Path) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    _write_workflow(user, "dup", "user version")
    _write_workflow(project, "dup", "project version")
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20), WorkflowDir(project, "project", 40)])
    (spec,) = reg.specs()
    assert spec.description == "project version"


def test_invalid_workflow_is_skipped(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_workflow(user, "good", "a valid one")
    bad = user / "bad"
    bad.mkdir(parents=True)
    (bad / "WORKFLOW.md").write_text("no frontmatter", encoding="utf-8")
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20)])
    assert [s.name for s in reg.specs()] == ["good"]


def test_get_returns_spec(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_workflow(user, "deploy-app", "Deploy application.")
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20)])
    spec = reg.get("deploy-app")
    assert spec is not None
    assert spec.name == "deploy-app"
    assert spec.description == "Deploy application."


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    reg = WorkflowRegistry([WorkflowDir(tmp_path / "user", "user", 20)])
    assert reg.get("nope") is None


def test_new_workflow_picked_up_without_restart(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    reg = WorkflowRegistry([WorkflowDir(user, "user", 20)])
    assert reg.specs() == []
    _write_workflow(user, "fresh", "authored mid-session")
    assert [s.name for s in reg.specs()] == ["fresh"]  # freshness re-scan


def test_default_workflow_dirs_ranking(tmp_path: Path) -> None:
    dirs = default_workflow_dirs(tmp_path / "home", tmp_path / "proj")
    by_source = {d.source: d for d in dirs}
    assert by_source["project"].rank > by_source["user"].rank
    assert by_source["project"].path == tmp_path / "proj" / ".jack" / "workflows"
    assert by_source["user"].path == tmp_path / "home" / ".autobot" / "workflows"
