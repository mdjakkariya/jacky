"""Autobot: a local, privacy-first, English-only voice assistant.

Everything runs on-device; no audio, text, or memory leaves the machine.
The package is organized around small, swappable component interfaces (see
:mod:`autobot.core.interfaces`) so models and back-ends can change as a config
choice rather than a rewrite. See ``docs/architecture/design-reference.md`` for
the design reference and ``CLAUDE.md`` for working conventions.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.8.0"
