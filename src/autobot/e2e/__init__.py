"""Dev-only E2E harness: drive the real `jack` TUI over a PTY and verify use cases.

Not shipped behavior and never run in CI — invoked by hand via ``make e2e`` /
``python -m autobot.e2e``. Requires the ``e2e`` extra (``uv sync --extra e2e``).
"""

from __future__ import annotations
