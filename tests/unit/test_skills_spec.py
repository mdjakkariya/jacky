"""Pure SKILL.md frontmatter parsing + validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.skills.spec import (
    SkillError,
    SkillSpec,
    parse_frontmatter,
    spec_from_text,
    validate_description,
    validate_name,
)

VALID = """---
name: pdf-tools
description: Extract text from PDFs. Use when the user mentions PDFs or forms.
---

# PDF Tools

Step one.
"""


def test_parse_frontmatter_splits_meta_and_body() -> None:
    meta, body = parse_frontmatter(VALID)
    assert meta["name"] == "pdf-tools"
    assert body.startswith("# PDF Tools")


def test_parse_frontmatter_missing_fence_raises() -> None:
    with pytest.raises(SkillError):
        parse_frontmatter("no frontmatter here")


def test_parse_frontmatter_unterminated_raises() -> None:
    with pytest.raises(SkillError):
        parse_frontmatter("---\nname: x\nno closing fence")


def test_validate_name_rejects_uppercase() -> None:
    with pytest.raises(SkillError):
        validate_name("PDF-Tools")


def test_validate_name_rejects_too_long() -> None:
    with pytest.raises(SkillError):
        validate_name("a" * 65)


@pytest.mark.parametrize("bad", ["anthropic-helper", "my-claude-skill"])
def test_validate_name_rejects_reserved_words(bad: str) -> None:
    with pytest.raises(SkillError):
        validate_name(bad)


def test_validate_name_accepts_good() -> None:
    assert validate_name("pdf-tools-2") == "pdf-tools-2"


def test_validate_description_rejects_empty() -> None:
    with pytest.raises(SkillError):
        validate_description("   ")


def test_validate_description_rejects_too_long() -> None:
    with pytest.raises(SkillError):
        validate_description("x" * 1025)


def test_validate_description_rejects_xml() -> None:
    with pytest.raises(SkillError):
        validate_description("do <thing>this</thing>")


def test_spec_from_text_builds_spec() -> None:
    spec = spec_from_text(VALID, path=Path("/skills/pdf-tools/SKILL.md"), source="user")
    assert isinstance(spec, SkillSpec)
    assert spec.name == "pdf-tools"
    assert spec.source == "user"
    assert spec.path == Path("/skills/pdf-tools/SKILL.md")


def test_spec_from_text_invalid_raises() -> None:
    with pytest.raises(SkillError):
        spec_from_text("---\nname: BAD\ndescription: x\n---\nbody", path=Path("x"), source="user")
