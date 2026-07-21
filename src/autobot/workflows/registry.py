"""Discover ``WORKFLOW.md`` workflows on disk and serve their catalog.

A peer of :class:`autobot.skills.registry.SkillRegistry`, applied to *workflows*
rather than *skills*. Workflows are parsed fully at discovery (they're small).
The registry re-scans automatically when the workflow files change, so a workflow
authored mid-session appears without a restart.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.skills.spec import SkillError
from autobot.workflows.spec import WorkflowError, WorkflowSpec, parse_workflow

_log = get_logger("workflow")


@dataclass(frozen=True, slots=True)
class WorkflowDir:
    """One searched directory: where it is, a source label, and its precedence rank.

    Higher ``rank`` wins on a name clash. Project levels outrank user levels.
    """

    path: Path
    source: str
    rank: int


def default_workflow_dirs(home: Path, project_root: Path) -> list[WorkflowDir]:
    """The two standard workflow directories, lowest → highest precedence."""
    return [
        WorkflowDir(home / ".autobot" / "workflows", "user", 20),
        WorkflowDir(project_root / ".jack" / "workflows", "project", 40),
    ]


class WorkflowRegistry:
    """Holds discovered workflows; refreshes lazily when the workflow files change."""

    def __init__(self, dirs: list[WorkflowDir]) -> None:
        """Store the searched dirs and perform an initial scan."""
        self._dirs = dirs
        self._by_name: dict[str, WorkflowSpec] = {}
        self._sig: tuple[tuple[str, int], ...] | None = None
        self._ensure_fresh()

    def _signature(self) -> tuple[tuple[str, int], ...]:
        """A cheap fingerprint of every ``*/WORKFLOW.md`` (path + mtime) across all dirs."""
        sig: list[tuple[str, int]] = []
        for d in self._dirs:
            if not d.path.is_dir():
                continue
            for md in d.path.glob("*/WORKFLOW.md"):
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
        """Scan every dir (ascending rank) and parse workflows; higher rank wins."""
        by_name: dict[str, WorkflowSpec] = {}
        for d in sorted(self._dirs, key=lambda x: x.rank):
            if not d.path.is_dir():
                continue
            for md in sorted(d.path.glob("*/WORKFLOW.md")):
                try:
                    spec = parse_workflow(md.read_text(encoding="utf-8"), path=md)
                except (OSError, WorkflowError, SkillError) as exc:
                    _log.warning("skipping invalid workflow at %s: %s", md, exc)
                    continue
                by_name[spec.name] = spec  # later (higher-rank) dir overwrites
        self._by_name = by_name
        _log.info("workflow catalog loaded count=%d", len(by_name))

    def specs(self) -> list[WorkflowSpec]:
        """All discovered workflows, name-sorted (refreshing first if files changed)."""
        self._ensure_fresh()
        return sorted(self._by_name.values(), key=lambda s: s.name)

    def catalog(self) -> str:
        """The tier-1 catalog block for the system prompt, or ``""`` if no workflows."""
        specs = self.specs()
        if not specs:
            return ""
        lines = [
            'Available workflows — deterministic recipes. Run one with run_workflow("'
            '<name>", {...inputs}) when the task matches:'
        ]
        lines += [f"- {s.name}: {s.description}" for s in specs]
        return "\n".join(lines)

    def get(self, name: str) -> WorkflowSpec | None:
        """A discovered workflow by name, or ``None`` if unknown."""
        self._ensure_fresh()
        return self._by_name.get(name)
