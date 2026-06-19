"""Immutable value types passed between pipeline stages.

These are deliberately plain ``dataclasses`` with no behaviour: they form the
stable vocabulary that the component interfaces speak, so a change of model or
back-end never ripples into the rest of the system.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

AudioClip = npt.NDArray[np.float32]
"""A captured utterance: 1-D ``float32`` mono PCM at the pipeline sample rate."""


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
