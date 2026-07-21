"""SkillRegistry discovery, precedence, freshness, catalog, and body loading."""

from __future__ import annotations

from pathlib import Path

from autobot.skills.registry import SkillDir, SkillRegistry, default_skill_dirs


def _write_skill(root: Path, name: str, description: str, body: str = "Do the thing.") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    return md


def test_discovers_skill(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_skill(user, "pdf-tools", "Extract PDF text. Use for PDFs.")
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    names = [s.name for s in reg.specs()]
    assert names == ["pdf-tools"]


def test_catalog_empty_when_no_skills(tmp_path: Path) -> None:
    reg = SkillRegistry([SkillDir(tmp_path / "nope", "user", 20)])
    assert reg.catalog() == ""


def test_catalog_lists_name_and_description(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_skill(user, "pdf-tools", "Extract PDF text. Use for PDFs.")
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    cat = reg.catalog()
    assert "pdf-tools" in cat and "Extract PDF text" in cat
    assert 'skill("' in cat  # tells the model how to activate


def test_project_overrides_user(tmp_path: Path) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    _write_skill(user, "dup", "user version")
    _write_skill(project, "dup", "project version")
    reg = SkillRegistry([SkillDir(user, "user", 20), SkillDir(project, "project", 40)])
    (spec,) = reg.specs()
    assert spec.description == "project version"
    assert spec.source == "project"


def test_body_returns_content(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_skill(user, "pdf-tools", "Extract PDF text.", body="# PDF\n\nStep one.")
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    body = reg.body("pdf-tools")
    assert body is not None and "Step one." in body


def test_body_unknown_returns_none(tmp_path: Path) -> None:
    reg = SkillRegistry([SkillDir(tmp_path / "user", "user", 20)])
    assert reg.body("nope") is None


def test_discovery_accepts_placeholder_and_colon_descriptions(tmp_path: Path) -> None:
    """Fix 1 + Fix 2: real ecosystem skills are no longer silently dropped."""
    user = tmp_path / "user"
    _write_skill(user, "spindown", "Use `<branch>` --only extension|router")
    _write_skill(user, "spinup", "For new tasks: creates things")
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    names = {s.name for s in reg.specs()}
    assert names == {"spindown", "spinup"}


def test_invalid_skill_is_skipped(tmp_path: Path) -> None:
    user = tmp_path / "user"
    _write_skill(user, "good", "a valid one")
    bad = user / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    assert [s.name for s in reg.specs()] == ["good"]


def test_new_skill_picked_up_without_restart(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    reg = SkillRegistry([SkillDir(user, "user", 20)])
    assert reg.specs() == []
    _write_skill(user, "fresh", "authored mid-session")
    assert [s.name for s in reg.specs()] == ["fresh"]  # freshness re-scan


def test_default_skill_dirs_ranking(tmp_path: Path) -> None:
    dirs = default_skill_dirs(tmp_path / "home", tmp_path / "proj")
    by_source = {d.source: d for d in dirs}
    assert by_source["project"].rank > by_source["user"].rank
    assert by_source["user"].rank > by_source["compat-user"].rank
    assert by_source["project"].path == tmp_path / "proj" / ".jack" / "skills"
    assert by_source["compat-user"].path == tmp_path / "home" / ".claude" / "skills"
