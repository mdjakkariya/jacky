"""Apply settings for one E2E run without mutating the user's real config.

Snapshots ``~/.autobot/settings.json`` (or its absence), sparse-merges the run's overrides
(e.g. the scenario's ``coding_autonomy``), and restores the exact prior state in a
``finally`` — so benchmarking never leaves the user's autonomy/model changed. Reuses the
shared config helpers (``read_settings``/``write_settings``) for the merge/write, which
gives malformed-JSON tolerance and a 0600 chmod for free; the restore itself stays a
byte-exact rewrite of whatever was on disk before.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from autobot.config import DEFAULT_SETTINGS_PATH, read_settings, write_settings
from autobot.logging_setup import get_logger

_log = get_logger("e2e")


@contextmanager
def settings_scope(
    updates: dict[str, object], *, path: str = DEFAULT_SETTINGS_PATH
) -> Iterator[None]:
    """Apply ``updates`` to the settings file for the duration, then restore it exactly."""
    p = Path(path).expanduser()
    original: str | None = p.read_text(encoding="utf-8") if p.exists() else None
    merged = {**read_settings(path), **updates}  # tolerant parse of the current file
    write_settings(merged, path)  # atomic write + 0600 perms
    _log.info("settings_scope applied keys=%s", sorted(updates))
    try:
        yield
    finally:
        if original is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(original, encoding="utf-8")  # byte-exact restore
        _log.info("settings_scope restored")
