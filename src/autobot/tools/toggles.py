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

    def set_volume(self, level: int | None = None, action: str | None = None) -> str:
        """Set the system volume (0-100) or mute/unmute/nudge it up/down."""
        if action in ("mute", "unmute"):
            muted = "true" if action == "mute" else "false"
            rc, out = self._run(["osascript", "-e", f"set volume output muted {muted}"])
            if rc != 0:
                return f"I couldn't change the volume: {out or 'unknown error'}"
            _log.info("volume muted=%s", muted)
            return "Muted." if action == "mute" else "Unmuted."
        if action in ("up", "down"):
            rc, out = self._run(["osascript", "-e", "output volume of (get volume settings)"])
            current = first_int(out)
            if rc != 0 or current is None:
                return "I couldn't read the current volume."
            level = clamp(current + (_VOLUME_STEP if action == "up" else -_VOLUME_STEP))
        if level is None:
            return "Tell me a level (0-100), or whether to turn it up, down, or mute."
        level = clamp(level)
        rc, out = self._run(["osascript", "-e", f"set volume output volume {level}"])
        if rc != 0:
            return f"I couldn't set the volume: {out or 'unknown error'}"
        _log.info("volume set to=%d", level)
        return f"Volume set to {level}%."

    def set_brightness(self, level: int | None = None, action: str | None = None) -> str:
        """Set screen brightness; degrade gracefully when no native path is available."""
        if action in ("up", "down"):
            key = 144 if action == "up" else 145
            rc, out = self._run(
                ["osascript", "-e", f'tell application "System Events" to key code {key}']
            )
            if rc != 0:
                if is_accessibility_error(out):
                    return (
                        "I need Accessibility access to adjust brightness this way. Enable "
                        "Jack under System Settings → Privacy & Security → Accessibility."
                    )
                return f"I couldn't change the brightness: {out or 'unknown error'}"
            _log.info("brightness action=%s", action)
            return f"Brightness turned {action}."
        if level is None:
            return "Tell me a level (0-100), or whether to make it brighter or dimmer."
        level = clamp(level)
        rc, out = self._run(["brightness", str(level / 100)])
        if rc == 127:  # the brightness binary isn't installed
            return (
                "I can make the screen brighter or dimmer step by step. For an exact level, "
                "install the brightness tool: run `brew install brightness`."
            )
        if rc != 0:
            return f"I couldn't set the brightness: {out or 'unknown error'}"
        _log.info("brightness set to=%d", level)
        return f"Brightness set to {level}%."

    def set_appearance(self, mode: str) -> str:
        """Switch the system appearance to dark, light, or the opposite of now."""
        mode = (mode or "").lower()
        base = 'tell application "System Events" to tell appearance preferences'
        if mode == "toggle":
            expr = f"{base} to set dark mode to not dark mode"
        elif mode in ("dark", "light"):
            value = "true" if mode == "dark" else "false"
            expr = f"{base} to set dark mode to {value}"
        else:
            return "Say 'dark', 'light', or 'toggle'."
        rc, out = self._run(["osascript", "-e", expr])
        if rc != 0:
            return f"I couldn't change the appearance: {out or 'unknown error'}"
        read_rc, now = self._run(["osascript", "-e", f"{base} to return dark mode"])
        if read_rc != 0:
            # The change applied; we just couldn't confirm the new state.
            if mode in ("dark", "light"):
                _log.info("appearance mode=%s (read-back failed)", mode)
                return f"Now in {mode} mode."
            _log.info("appearance toggled (read-back failed)")
            return "Done — I switched the appearance."
        is_dark = now.strip().lower() == "true"
        _log.info("appearance dark=%s", is_dark)
        return "Now in dark mode." if is_dark else "Now in light mode."
