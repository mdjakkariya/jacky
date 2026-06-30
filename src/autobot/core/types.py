"""Immutable value types passed between pipeline stages.

These are deliberately plain ``dataclasses`` with no behaviour: they form the
stable vocabulary that the component interfaces speak, so a change of model or
back-end never ripples into the rest of the system.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

AudioClip = npt.NDArray[np.float32]
"""A captured utterance: 1-D ``float32`` mono PCM at the pipeline sample rate."""

Int16Frame = npt.NDArray[np.int16]
"""A frame of 16-bit integer PCM samples (what the wake-word model consumes)."""


class Risk(enum.IntEnum):
    """How dangerous a tool invocation is, used by the permission gate.

    Ordered so that ``>=`` comparisons express "at least this risky". The gate
    introduced in Phase 1 confirms anything at or above ``DESTRUCTIVE`` and
    audits everything.
    """

    READ_ONLY = 0
    """No side effects (e.g. reading the time or a file)."""

    WRITE = 1
    """Creates or modifies state, but is reversible (e.g. create a file)."""

    DESTRUCTIVE = 2
    """Irreversible or hard to undo (e.g. delete, overwrite, network send)."""


@dataclass(frozen=True, slots=True)
class Transcription:
    """The result of speech-to-text on a single audio clip."""

    text: str
    """The recognized text, normalized and stripped. Empty if nothing heard."""

    confidence: float
    """Rough 0..1 confidence; 0.0 when no speech was detected."""

    @property
    def is_empty(self) -> bool:
        """Whether the transcription contains no usable text."""
        return not self.text


@dataclass(frozen=True, slots=True)
class Segment:
    """One timestamped span of recognized speech (seconds from the stream start)."""

    text: str
    """The recognized text for this span, stripped. Never empty in a returned list."""

    start: float
    """Start time in seconds, relative to the start of the transcribed audio."""

    end: float
    """End time in seconds, relative to the start of the transcribed audio."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke a named tool with arguments."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of dispatching a :class:`ToolCall`."""

    name: str
    content: str
    ok: bool = True


# A function that takes a planned tool call and returns its result. The
# orchestrator wires this to the permission gate, so the language model can drive
# tool execution without ever touching the gate (or side effects) directly.
ToolExecutor = Callable[["ToolCall"], "ToolResult"]


class Decision(enum.Enum):
    """The permission gate's ruling on a tool invocation, recorded in the audit log."""

    ALLOWED = "allowed"
    """The call was permitted and executed (see ``ok`` for the execution result)."""

    DENIED = "denied"
    """The call was blocked (unknown tool, or the user declined confirmation)."""


class State(enum.Enum):
    """States of the orchestrator's interaction loop.

    The backbone the whole assistant plugs into. UIs observe transitions to show
    what the assistant is doing; later phases add streaming and barge-in without
    changing this vocabulary.
    """

    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    PLANNING = "planning"
    EXECUTING = "executing"
    RESPONDING = "responding"
    CLARIFYING = "clarifying"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable record of a gate decision, written to the audit log."""

    timestamp: str
    """ISO-8601 UTC timestamp of the decision."""

    tool: str
    arguments: dict[str, Any]
    risk: str
    """The tool's :class:`Risk` level by name (``"unknown"`` if unregistered)."""

    decision: Decision
    ok: bool | None
    """Execution success when allowed; ``None`` when denied."""

    detail: str
    """Result content or the reason a call was denied."""
