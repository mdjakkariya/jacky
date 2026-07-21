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
_FENCE_RE = re.compile(r"^---[ \t]*$")
_XML_RE = re.compile(r"<[^>]+>")
_KV_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]?(.*)$")
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


def _lenient_meta(yaml_text: str) -> dict[str, Any]:
    """Recover a best-effort ``{key: value}`` mapping from a frontmatter block.

    Used as a fallback when the block isn't valid (or isn't mapping-shaped) YAML —
    real-world descriptions often contain an embedded ``colon: space`` (e.g. "For
    new tasks: creates things") that makes ``yaml.safe_load`` misparse the line as a
    nested mapping. Each line is matched independently, so multi-line YAML
    constructs (block scalars, nested mappings) are not reconstructed here — this
    is a discovery-time safety net, not a YAML parser.

    Args:
        yaml_text: The raw text between the frontmatter fences.

    Returns:
        A mapping of whatever ``key: value`` lines matched; ``{}`` if none did.
    """
    meta: dict[str, Any] = {}
    for line in yaml_text.split("\n"):
        match = _KV_RE.match(line)
        if match:
            meta[match.group(1)] = match.group(2).strip()
    return meta


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` into its frontmatter mapping and Markdown body.

    Liberal on discovery: frontmatter that isn't valid YAML (or doesn't parse to a
    mapping) no longer raises here — it falls back to a line-based ``key: value``
    parse (see :func:`_lenient_meta`) so a real-world description like "For new
    tasks: creates things" still yields a usable ``name``/``description``. Only the
    fence structure itself is still load-bearing enough to raise.

    Args:
        text: The full file contents.

    Returns:
        A ``(metadata, body)`` tuple; ``body`` has its leading newlines trimmed.

    Raises:
        SkillError: If there is no leading ``---`` fence or no closing ``---``.
    """
    stripped = text.lstrip("﻿ \t\r\n")
    # Normalize CRLF and CR line endings to LF
    stripped = stripped.replace("\r\n", "\n").replace("\r", "\n")
    lines = stripped.split("\n")

    # Check for opening fence
    if not _FENCE_RE.match(lines[0]):
        raise SkillError("missing YAML frontmatter (no leading '---')")

    # Find closing fence
    closing_idx = None
    for i in range(1, len(lines)):
        if _FENCE_RE.match(lines[i]):
            closing_idx = i
            break

    if closing_idx is None:
        raise SkillError("unterminated frontmatter (missing closing '---')")

    # Extract and parse YAML, falling back to a lenient line parse on bad content
    yaml_text = "\n".join(lines[1:closing_idx])
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        parsed = None
    meta = parsed if isinstance(parsed, dict) else _lenient_meta(yaml_text)

    # Extract body (everything after closing fence)
    body = "\n".join(lines[closing_idx + 1 :]).lstrip("\n")
    return meta, body


def validate_name(name: object) -> str:
    """Return ``name`` if it satisfies the standard's rules, else raise ``SkillError``."""
    if not isinstance(name, str) or not name:
        raise SkillError("name is required")
    if len(name) > _NAME_MAX:
        raise SkillError(f"name exceeds {_NAME_MAX} characters")
    if not _NAME_RE.fullmatch(name):
        raise SkillError("name must be lowercase letters, digits, and hyphens only")
    if any(word in name for word in _RESERVED):
        raise SkillError("name must not contain a reserved word (anthropic/claude)")
    return name


def validate_description(description: object, *, strict: bool = True) -> str:
    """Return ``description`` if valid, else raise ``SkillError``.

    Args:
        description: The raw frontmatter value.
        strict: When ``True`` (the default, used for authoring), angle-bracket
            "XML tag" content is rejected. Discovery calls this with ``strict=False``
            so a real description like "use ``<branch>``" isn't silently dropped —
            it's a placeholder, not an XML tag, and only an author-time concern.

    Raises:
        SkillError: If the description is missing, empty, too long, or (when
            ``strict``) contains angle-bracket content.
    """
    if not isinstance(description, str) or not description.strip():
        raise SkillError("description is required")
    if len(description) > _DESC_MAX:
        raise SkillError(f"description exceeds {_DESC_MAX} characters")
    if strict and _XML_RE.search(description):
        raise SkillError("description must not contain XML tags")
    return description


def spec_from_text(text: str, *, path: Path, source: str, strict: bool = True) -> SkillSpec:
    """Parse + validate a ``SKILL.md``'s text into a :class:`SkillSpec`.

    Args:
        text: The full ``SKILL.md`` contents.
        path: The file's location, stored on the resulting spec.
        source: The precedence-level label (e.g. ``"project"``, ``"user"``).
        strict: Forwarded to :func:`validate_description`; ``False`` for discovery
            (liberal), ``True`` for authoring (strict). ``validate_name`` is always
            strict — the catalog key and reserved-word rule are non-negotiable.

    Raises:
        SkillError: If parsing or validation fails.
    """
    meta, _ = parse_frontmatter(text)
    name = validate_name(meta.get("name"))
    description = validate_description(meta.get("description"), strict=strict)
    return SkillSpec(name=name, description=description, path=path, source=source)
