"""Write-side system controls — change common macOS settings by voice/chat.

The mirror of :mod:`autobot.tools.system`'s read-only status tools: volume,
brightness, appearance (dark/light), sleep, Wi-Fi, keep-awake, and lock screen.
All are ``WRITE`` (audited, no confirmation — each is instantly reversible) and run
entirely on-device via ``osascript`` / ``pmset`` / ``networksetup`` / ``caffeinate``.
Fragile paths (brightness, Wi-Fi, lock) degrade gracefully and never escalate to
``sudo``.

A ``Runner`` is injected for one-shot commands and a ``ProcessManager`` for
``caffeinate``'s background process, so the whole module is unit-tested against
canned output with no real hardware.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable
from typing import Protocol

from autobot.logging_setup import get_logger

_log = get_logger("toggles")

RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]

_VOLUME_STEP = 10  # how much "louder"/"quieter" nudges the level
_WIFI_IF = "en0"  # Wi-Fi interface fallback when it can't be resolved
# The classic CLI lock path; absent on newer macOS, where we fall back to a keystroke.
_CGSESSION = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"


def _subprocess_runner(args: list[str]) -> RunResult:
    """Default runner: run ``args`` (no shell) and return (code, output)."""
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def clamp(level: int) -> int:
    """Clamp a percentage to the 0-100 range."""
    return max(0, min(100, level))


def first_int(out: str) -> int | None:
    """Return the first integer found in ``out``, or ``None``."""
    match = re.search(r"-?\d+", out)
    return int(match.group()) if match else None


def is_accessibility_error(out: str) -> bool:
    """Whether command output looks like a denied-Accessibility AppleScript error."""
    low = out.lower()
    return "-1719" in out or "not allowed" in low or "assistive" in low or "accessibility" in low


class ProcessManager(Protocol):
    """Spawns and stops a long-running background process (for ``caffeinate``)."""

    def start(self, argv: list[str]) -> int:
        """Spawn ``argv`` detached and return its pid."""
        ...

    def stop(self, pid: int) -> None:
        """Terminate the process with ``pid`` (no error if already gone)."""
        ...


class _SubprocessManager:
    """Default :class:`ProcessManager`: ``Popen`` to start, ``SIGTERM`` to stop."""

    def start(self, argv: list[str]) -> int:
        """Spawn ``argv`` with output discarded and return its pid."""
        import subprocess

        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.pid

    def stop(self, pid: int) -> None:
        """Send ``SIGTERM`` to ``pid``; ignore if it has already exited."""
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


class SystemToggles:
    """Write-side macOS controls exposed as tools."""

    def __init__(self, runner: Runner | None = None, procs: ProcessManager | None = None) -> None:
        """Store the injected command runner and process manager."""
        self._run = runner or _subprocess_runner
        self._procs = procs or _SubprocessManager()
        self._awake_pid: int | None = None
