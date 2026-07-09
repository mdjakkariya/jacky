"""Declarative scenarios: what to seed, how to drive, and how to verify.

A scenario is a real user task run through the TUI. ``strategy`` picks the driver:
``"unattended"`` sends ``task`` and auto-approves any gate; ``"scripted"`` runs ``steps``
verbatim (to exercise the plan/permission cards deliberately). ``checks`` are deterministic
(machine-verified) ground truth; ``success_criteria`` is the natural-language rubric the LLM
judge uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_AUTONOMY = ("plan", "confirm", "auto")
_STRATEGY = ("scripted", "unattended")


@dataclass(frozen=True, slots=True)
class Send:
    """Type a line into the prompt (a newline is appended by the driver)."""

    text: str


@dataclass(frozen=True, slots=True)
class Key:
    """Press a single key by name (e.g. ``"1"``, ``"enter"``)."""

    name: str


@dataclass(frozen=True, slots=True)
class Expect:
    """Wait until a named marker holds on screen, or fail after ``timeout`` seconds."""

    marker: str
    timeout: float = 60.0


@dataclass(frozen=True, slots=True)
class ApprovePlan:
    """Wait for the plan card, then approve it (``1`` + Enter)."""


@dataclass(frozen=True, slots=True)
class Confirm:
    """Wait for the permission card, then approve it (``1`` + Enter)."""


Action = Send | Key | Expect | ApprovePlan | Confirm


@dataclass(frozen=True, slots=True)
class FileExists:
    """Deterministic check: a workspace-relative path exists after the run."""

    path: str


@dataclass(frozen=True, slots=True)
class FileContains:
    """Deterministic check: a workspace file exists and contains ``needle``."""

    path: str
    needle: str


@dataclass(frozen=True, slots=True)
class FileLacks:
    """Deterministic check: a workspace file exists but does NOT contain ``needle``.

    The anchor for reversions (e.g. ``/undo``): a missing file is *not* a pass, since that
    proves nothing about whether the edit was rolled back.
    """

    path: str
    needle: str


@dataclass(frozen=True, slots=True)
class ScreenContains:
    """Deterministic check: the final rendered screen contains ``needle``."""

    needle: str


Check = FileExists | FileContains | FileLacks | ScreenContains


@dataclass(frozen=True, slots=True)
class Scenario:
    """One real-world use case: seed → drive → verify."""

    name: str
    autonomy: str
    strategy: str
    task: str
    success_criteria: str
    seed_files: dict[str, str] = field(default_factory=dict)
    steps: tuple[Action, ...] = ()
    checks: tuple[Check, ...] = ()

    def validate(self) -> None:
        """Raise ``ValueError`` if the scenario is internally inconsistent."""
        if self.autonomy not in _AUTONOMY:
            raise ValueError(f"autonomy must be one of {_AUTONOMY}, got {self.autonomy!r}")
        if self.strategy not in _STRATEGY:
            raise ValueError(f"strategy must be one of {_STRATEGY}, got {self.strategy!r}")
        if self.strategy == "scripted" and not self.steps:
            raise ValueError("a scripted scenario needs at least one step in steps")
        if not self.task.strip():
            raise ValueError("task must be non-empty")
