"""Pure parsing and validation for a standard ``SKILL.md`` file.

No filesystem I/O lives here so the whole module is trivially unit-testable: the
registry reads bytes off disk and hands the text in. The validation rules mirror
the open Agent Skills standard (name/description constraints).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_XML_RE = re.compile(r"<[^>]+>")
_RESERVED = ("anthropic", "claude")
_NAME_MAX = 64
_DESC_MAX = 1024


class SkillError(Exception):
    """A ``SKILL.md`` is malformed or violates the standard's metadata rules."""


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """A discovered skill's identity and location (frontmatter only, no body)."""

    name: str
    description: str
    path: Path
    source: str


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` into its YAML frontmatter mapping and Markdown body.

    Args:
        text: The full file contents.

    Returns:
        A ``(metadata, body)`` tuple; ``body`` has its leading blank lines trimmed.

    Raises:
        SkillError: If there is no leading ``---`` fence, no closing ``---``, the
            YAML is invalid, or the frontmatter is not a mapping.
    """
    stripped = text.lstrip("﻿ \t\r\n")
    if not stripped.startswith("---"):
        raise SkillError("missing YAML frontmatter (no leading '---')")
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        raise SkillError("unterminated frontmatter (missing closing '---')")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError("frontmatter is not a mapping")
    return meta, parts[2].lstrip("\n")


def validate_name(name: object) -> str:
    """Return ``name`` if it satisfies the standard's rules, else raise ``SkillError``."""
    if not isinstance(name, str) or not name:
        raise SkillError("name is required")
    if len(name) > _NAME_MAX:
        raise SkillError(f"name exceeds {_NAME_MAX} characters")
    if not _NAME_RE.match(name):
        raise SkillError("name must be lowercase letters, digits, and hyphens only")
    if any(word in name for word in _RESERVED):
        raise SkillError("name must not contain a reserved word (anthropic/claude)")
    return name


def validate_description(description: object) -> str:
    """Return ``description`` if valid, else raise ``SkillError``."""
    if not isinstance(description, str) or not description.strip():
        raise SkillError("description is required")
    if len(description) > _DESC_MAX:
        raise SkillError(f"description exceeds {_DESC_MAX} characters")
    if _XML_RE.search(description):
        raise SkillError("description must not contain XML tags")
    return description


def spec_from_text(text: str, *, path: Path, source: str) -> SkillSpec:
    """Parse + validate a ``SKILL.md``'s text into a :class:`SkillSpec`.

    Raises:
        SkillError: If parsing or validation fails.
    """
    meta, _ = parse_frontmatter(text)
    name = validate_name(meta.get("name"))
    description = validate_description(meta.get("description"))
    return SkillSpec(name=name, description=description, path=path, source=source)
