"""Discover ``SKILL.md`` skills on disk and serve their catalog + bodies.

A peer of :class:`autobot.tools.registry.ToolRegistry`, applied to *instructions*
rather than *tools*. Only frontmatter is parsed at discovery (tier 1); a skill's
full body is read on demand (tier 2). The registry re-scans automatically when the
skill files change, so a skill authored mid-session appears without a restart.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.skills.spec import SkillError, SkillSpec, parse_frontmatter, spec_from_text

_log = get_logger("skills")


@dataclass(frozen=True, slots=True)
class SkillDir:
    """One searched directory: where it is, a source label, and its precedence rank.

    Higher ``rank`` wins on a name clash. Project levels outrank user levels;
    within a level, canonical dirs outrank the ``.claude`` compatibility dirs.
    """

    path: Path
    source: str
    rank: int


def default_skill_dirs(home: Path, project_root: Path) -> list[SkillDir]:
    """The four standard skill directories, lowest → highest precedence."""
    return [
        SkillDir(home / ".claude" / "skills", "compat-user", 10),
        SkillDir(home / ".autobot" / "skills", "user", 20),
        SkillDir(project_root / ".claude" / "skills", "compat-project", 30),
        SkillDir(project_root / ".jack" / "skills", "project", 40),
    ]


class SkillRegistry:
    """Holds discovered skills; refreshes lazily when the skill files change."""

    def __init__(self, dirs: list[SkillDir]) -> None:
        """Store the searched dirs and perform an initial scan."""
        self._dirs = dirs
        self._by_name: dict[str, SkillSpec] = {}
        self._sig: tuple[tuple[str, int], ...] | None = None
        self._ensure_fresh()

    def _signature(self) -> tuple[tuple[str, int], ...]:
        """A cheap fingerprint of every ``*/SKILL.md`` (path + mtime) across all dirs."""
        sig: list[tuple[str, int]] = []
        for d in self._dirs:
            if not d.path.is_dir():
                continue
            for md in d.path.glob("*/SKILL.md"):
                with contextlib.suppress(OSError):
                    sig.append((str(md), md.stat().st_mtime_ns))
        return tuple(sorted(sig))

    def _ensure_fresh(self) -> None:
        """Re-scan only if the filesystem fingerprint changed since the last scan."""
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            self.reload()

    def reload(self) -> None:
        """Scan every dir (ascending rank) and parse frontmatter; higher rank wins."""
        by_name: dict[str, SkillSpec] = {}
        for d in sorted(self._dirs, key=lambda x: x.rank):
            if not d.path.is_dir():
                continue
            for md in sorted(d.path.glob("*/SKILL.md")):
                try:
                    spec = spec_from_text(
                        md.read_text(encoding="utf-8"), path=md, source=d.source, strict=False
                    )
                except (OSError, SkillError) as exc:
                    _log.warning("skipping invalid skill at %s: %s", md, exc)
                    continue
                by_name[spec.name] = spec  # later (higher-rank) dir overwrites
        self._by_name = by_name
        _log.info("skills catalog loaded count=%d", len(by_name))

    def specs(self) -> list[SkillSpec]:
        """All discovered skills, name-sorted (refreshing first if files changed)."""
        self._ensure_fresh()
        return sorted(self._by_name.values(), key=lambda s: s.name)

    def catalog(self) -> str:
        """The tier-1 catalog block for the system prompt, or ``""`` if no skills."""
        specs = self.specs()
        if not specs:
            return ""
        lines = [
            "Available skills — reusable playbooks loaded on demand. When the task "
            'matches one, call skill("<name>") to load its full instructions before acting:'
        ]
        lines += [f"- {s.name}: {s.description}" for s in specs]
        return "\n".join(lines)

    def skill_dir(self, name: str) -> Path | None:
        """The directory a discovered skill lives in, or ``None`` if unknown.

        Used to path-jail tier-3 reference-file reads (``read_skill_file``) to the
        skill's own directory, wherever it was discovered from.
        """
        self._ensure_fresh()
        spec = self._by_name.get(name)
        return None if spec is None else spec.path.parent

    def body(self, name: str) -> str | None:
        """The full Markdown body of a skill, or ``None`` if unknown/unreadable."""
        self._ensure_fresh()
        spec = self._by_name.get(name)
        if spec is None:
            return None
        try:
            _, body = parse_frontmatter(spec.path.read_text(encoding="utf-8"))
        except (OSError, SkillError) as exc:
            _log.warning("skill body unreadable name=%r: %s", name, exc)
            return None
        return body.strip()
