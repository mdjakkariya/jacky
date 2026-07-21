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


def test_validate_description_strict_false_allows_angle_brackets() -> None:
    """Fix 1: discovery (strict=False) accepts a placeholder like `<branch>`."""
    desc = "Use `<branch>` --only extension|router"
    assert validate_description(desc, strict=False) == desc


def test_spec_from_text_builds_spec() -> None:
    spec = spec_from_text(VALID, path=Path("/skills/pdf-tools/SKILL.md"), source="user")
    assert isinstance(spec, SkillSpec)
    assert spec.name == "pdf-tools"
    assert spec.source == "user"
    assert spec.path == Path("/skills/pdf-tools/SKILL.md")


def test_spec_from_text_invalid_raises() -> None:
    with pytest.raises(SkillError):
        spec_from_text("---\nname: BAD\ndescription: x\n---\nbody", path=Path("x"), source="user")


def test_spec_from_text_strict_false_allows_placeholder_and_colon() -> None:
    """Fix 1 + Fix 2 together: the two real-world shapes discovery must accept."""
    bracket_text = (
        "---\nname: spindown\ndescription: Use `<branch>` --only extension|router\n---\nbody"
    )
    spec = spec_from_text(bracket_text, path=Path("x"), source="user", strict=False)
    assert spec.description == "Use `<branch>` --only extension|router"

    colon_text = "---\nname: spinup\ndescription: For new tasks: creates things\n---\nbody"
    spec = spec_from_text(colon_text, path=Path("x"), source="user", strict=False)
    assert spec.description == "For new tasks: creates things"


def test_spec_from_text_strict_true_still_rejects_xml() -> None:
    """Default strict=True (author-time) unchanged: angle brackets still reject."""
    text = "---\nname: x\ndescription: do <thing>this</thing>\n---\nbody"
    with pytest.raises(SkillError):
        spec_from_text(text, path=Path("x"), source="user")


def test_validate_name_rejects_trailing_newline() -> None:
    """Fix 1: trailing newlines should be rejected."""
    with pytest.raises(SkillError):
        validate_name("pdf-tools\n")


def test_parse_frontmatter_embedded_colon_recovers() -> None:
    """Fix 2: a description with an embedded 'colon: space' no longer raises.

    Real skills (e.g. spinup, error-analysis-revision) have single-line
    descriptions like "For tasks: do it" that make ``yaml.safe_load`` see a nested
    mapping and raise. The line-based fallback recovers name/description instead.
    """
    meta, body = parse_frontmatter("---\nname: x\ndescription: For tasks: do it\n---\nbody")
    assert meta["name"] == "x"
    assert meta["description"] == "For tasks: do it"
    assert body == "body"


def test_spec_from_text_non_mapping_frontmatter_raises() -> None:
    """Fix 2: parse_frontmatter no longer raises on a list, but spec_from_text does.

    A YAML list falls back to an empty ``meta`` (no ``key: value`` lines match), so
    ``spec_from_text`` still rejects it downstream — for the missing ``name``, not
    for the frontmatter shape itself.
    """
    with pytest.raises(SkillError):
        spec_from_text("---\n- a\n- b\n---\nbody", path=Path("x"), source="user")


def test_parse_frontmatter_block_scalar_with_indented_fence() -> None:
    """Fix 2: block-scalar description containing indented '---' is preserved."""
    text = """---
name: pdf-tools
description: |
  Extract text from PDFs.
  The following is NOT a fence:
    --- this is indented
  But this is real body content.
---

# Real Body

Content here.
"""
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "pdf-tools"
    # The description should contain the full block-scalar content
    assert "NOT a fence" in meta["description"]
    assert "indented" in meta["description"]
    assert "real body content" in meta["description"]
    # Body should only contain the real body
    assert body.startswith("# Real Body")
    assert "Extract text" not in body


def test_parse_frontmatter_handles_crlf() -> None:
    """CRLF-terminated SKILL.md parses correctly."""
    text = (
        "---\r\nname: pdf-tools\r\ndescription: Extract PDF text.\r\n---\r\n\r\n"
        "# Body\r\n\r\nStep one.\r\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "pdf-tools"
    assert meta["description"] == "Extract PDF text."
    assert "\r" not in body
    assert body.startswith("# Body")
