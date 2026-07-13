"""Entry shim: the inline coding-agent REPL lives in ``autobot.cli.shell``.

Kept as ``tui.run`` so the ``jack`` composition root (``cli/__init__.py:main``) is unchanged.
"""

from __future__ import annotations

from autobot.cli.shell import run

__all__ = ["run"]
