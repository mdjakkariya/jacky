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

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.system import parse_wifi_device

_log = get_logger("toggles")

RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]
# A long-running installer (Homebrew); separate from Runner because `brew install`
# far exceeds the one-shot 10s command timeout.
Installer = Callable[[], RunResult]

_BREW_URL = "https://brew.sh"

_VOLUME_STEP = 10  # how much "louder"/"quieter" nudges the level
_WIFI_IF = "en0"  # Wi-Fi interface fallback when it can't be resolved
# The classic CLI lock path; absent on newer macOS, where we fall back to display sleep.
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


def _brew_install_brightness() -> RunResult:
    """Install the 'brightness' tool via Homebrew (a longer timeout than one-shots)."""
    import subprocess

    try:
        proc = subprocess.run(
            ["brew", "install", "brightness"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        return 127, "command not found: brew"
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

    def __init__(
        self,
        runner: Runner | None = None,
        procs: ProcessManager | None = None,
        installer: Installer | None = None,
    ) -> None:
        """Store the injected command runner, process manager, and brew installer."""
        self._run = runner or _subprocess_runner
        self._procs = procs or _SubprocessManager()
        self._install = installer or _brew_install_brightness
        self._awake_pid: int | None = None

    def specs(self) -> list[ToolSpec]:
        """Return the write-side control tool specs."""
        no_params: dict[str, object] = {"type": "object", "properties": {}, "required": []}
        return [
            ToolSpec(
                name="set_volume",
                description=(
                    "Change the Mac's output volume. Cues: 'set volume to 30', 'turn it "
                    "up/down', 'louder/quieter', 'mute', 'unmute'. Pass `level` (0-100) for an "
                    "exact level, or `action` = up | down | mute | unmute."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "description": "Exact volume, 0-100."},
                        "action": {
                            "type": "string",
                            "enum": ["mute", "unmute", "up", "down"],
                            "description": "Relative change or mute toggle.",
                        },
                    },
                    "required": [],
                },
                handler=self.set_volume,
                risk=Risk.WRITE,
                ack="Adjusting the volume.",
            ),
            ToolSpec(
                name="set_brightness",
                description=(
                    "Change the screen brightness. Cues: 'set brightness to 40', 'brighter', "
                    "'dimmer'. Pass `level` (0-100) for an exact level (needs the 'brightness' "
                    "tool installed), or `action` = up | down to nudge it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "description": "Exact brightness, 0-100."},
                        "action": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Nudge brighter or dimmer.",
                        },
                    },
                    "required": [],
                },
                handler=self.set_brightness,
                risk=Risk.WRITE,
                ack="Adjusting the brightness.",
            ),
            ToolSpec(
                name="install_brightness_tool",
                description=(
                    "Install the Homebrew 'brightness' command-line tool, which lets you set an "
                    "EXACT brightness level (e.g. 'set brightness to 40'). Use this ONLY after "
                    "set_brightness reports the tool is missing and the user agrees to install "
                    "it. It downloads from the internet via Homebrew (the user is asked to "
                    "confirm first). After it succeeds, call set_brightness again with the level "
                    "the user originally wanted."
                ),
                parameters=no_params,
                handler=self.install_brightness,
                risk=Risk.DESTRUCTIVE,
                confirm_prompt=(
                    "Install the 'brightness' tool via Homebrew? This downloads it from the "
                    "internet so I can set exact brightness levels."
                ),
                ack="Installing the brightness tool.",
            ),
            ToolSpec(
                name="set_appearance",
                description=(
                    "Switch the system look between dark and light. Cues: 'dark mode', 'go "
                    "light', 'switch appearance'. Pass `mode` = dark | light | toggle."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["dark", "light", "toggle"],
                            "description": "Target appearance.",
                        }
                    },
                    "required": ["mode"],
                },
                handler=self.set_appearance,
                risk=Risk.WRITE,
                requires=AUTOMATION,
                ack="Switching the appearance.",
            ),
            ToolSpec(
                name="sleep_mac",
                description=(
                    "Put the Mac to sleep right now. Cues: 'go to sleep', 'sleep the Mac'."
                ),
                parameters=no_params,
                handler=self.sleep_mac,
                risk=Risk.WRITE,
                ack="Going to sleep.",
            ),
            ToolSpec(
                name="set_wifi",
                description=(
                    "Turn Wi-Fi on or off. Cues: 'turn off Wi-Fi', 'enable Wi-Fi', 'toggle "
                    "Wi-Fi'. Pass `state` = on | off | toggle."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["on", "off", "toggle"],
                            "description": "Target Wi-Fi power state.",
                        }
                    },
                    "required": ["state"],
                },
                handler=self.set_wifi,
                risk=Risk.WRITE,
                ack="Updating Wi-Fi.",
            ),
            ToolSpec(
                name="keep_awake",
                description=(
                    "Stop the Mac from sleeping. Cues: 'keep my Mac awake', 'don't sleep for 30 "
                    "minutes', 'stop keeping awake'. Pass `minutes` for a timed window (omit for "
                    "indefinite), or `off` = true to let it sleep normally again."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "integer",
                            "description": "How long to stay awake; omit for indefinite.",
                        },
                        "off": {
                            "type": "boolean",
                            "description": "Set true to stop keeping the Mac awake.",
                        },
                    },
                    "required": [],
                },
                handler=self.keep_awake,
                risk=Risk.WRITE,
                ack="Keeping your Mac awake.",
            ),
            ToolSpec(
                name="lock_screen",
                description="Lock the screen right now. Cues: 'lock my screen', 'lock the Mac'.",
                parameters=no_params,
                handler=self.lock_screen,
                risk=Risk.WRITE,
                ack="Locking the screen.",
            ),
        ]

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
                "Setting an exact level needs the 'brightness' tool, which isn't installed. "
                "Want me to install it for you? I'll ask before downloading anything. "
                "Otherwise I can make the screen brighter or dimmer step by step."
            )
        if rc != 0:
            return f"I couldn't set the brightness: {out or 'unknown error'}"
        _log.info("brightness set to=%d", level)
        return f"Brightness set to {level}%."

    def install_brightness(self) -> str:
        """Install the Homebrew 'brightness' tool so exact levels work (gated).

        Idempotent and honest about its limits: reports if it's already installed,
        and if Homebrew is missing it points at the install page rather than
        attempting a large unattended bootstrap.
        """
        if self._run(["which", "brightness"])[0] == 0:
            return "The brightness tool is already installed — ask me to set an exact level."
        if self._run(["which", "brew"])[0] != 0:
            return (
                "I can't install it automatically because Homebrew isn't available. Install "
                f"Homebrew from {_BREW_URL}, then ask me again — or just say 'brighter' / "
                "'dimmer' to adjust without it."
            )
        rc, out = self._install()
        if rc != 0:
            return f"I couldn't install the brightness tool: {out or 'unknown error'}"
        _log.info("brightness tool installed")
        return "Installed the brightness tool — now tell me the exact level you want."

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

    def sleep_mac(self) -> str:
        """Put the Mac to sleep immediately (reversible — just wake it)."""
        rc, out = self._run(["pmset", "sleepnow"])
        if rc != 0:
            return f"I couldn't put the Mac to sleep: {out or 'unknown error'}"
        _log.info("sleeping")
        return "Going to sleep."

    def _wifi_device(self) -> str:
        """Resolve the Wi-Fi interface dynamically, falling back to en0."""
        rc, out = self._run(["networksetup", "-listallhardwareports"])
        if rc == 0:
            dev = parse_wifi_device(out)
            if dev:
                return dev
        return _WIFI_IF

    def set_wifi(self, state: str) -> str:
        """Turn Wi-Fi on, off, or toggle it. Never escalates to sudo."""
        state = (state or "").lower()
        dev = self._wifi_device()
        if state == "toggle":
            _rc, power = self._run(["networksetup", "-getairportpower", dev])
            state = "off" if "on" in power.lower() else "on"
        if state not in ("on", "off"):
            return "Say 'on', 'off', or 'toggle'."
        rc, out = self._run(["networksetup", "-setairportpower", dev, state])
        if rc != 0:
            low = out.lower()
            if "admin" in low or "administrator" in low or "permission" in low or "denied" in low:
                return (
                    "macOS needs admin rights to toggle Wi-Fi on this Mac, so I can't do it "
                    "automatically."
                )
            return f"I couldn't change Wi-Fi: {out or 'unknown error'}"
        _log.info("wifi state=%s", state)
        return f"Wi-Fi turned {state}."

    def keep_awake(self, minutes: int | None = None, off: bool = False) -> str:
        """Keep the Mac awake (optionally for N minutes), or stop doing so."""
        if off:
            if self._awake_pid is None:
                return "Your Mac wasn't being kept awake."
            self._procs.stop(self._awake_pid)
            self._awake_pid = None
            _log.info("keep_awake stopped")
            return "Okay, your Mac can sleep normally again."
        # Replace any existing keep-awake so we never leak caffeinate processes.
        if self._awake_pid is not None:
            self._procs.stop(self._awake_pid)
            self._awake_pid = None
        argv = ["caffeinate", "-dimsu"]
        if minutes is not None and minutes > 0:
            argv += ["-t", str(minutes * 60)]
        self._awake_pid = self._procs.start(argv)
        _log.info("keep_awake minutes=%s pid=%s", minutes, self._awake_pid)
        if minutes is not None and minutes > 0:
            unit = "minute" if minutes == 1 else "minutes"
            return f"I'll keep your Mac awake for {minutes} {unit}."
        return "I'll keep your Mac awake until you tell me to stop."

    def lock_screen(self) -> str:
        """Lock the screen; sleep the display where CGSession is unavailable.

        Deliberately avoids synthesizing a Ctrl-Cmd-Q keystroke: that is delivered
        to the *frontmost app* and, if the Control modifier is dropped, arrives as
        Cmd-Q and quits it (it closed Jack's own window in testing). Display sleep
        is global, needs no permission, and locks when a password is required after
        sleep (the macOS default).
        """
        rc, _out = self._run([_CGSESSION, "-suspend"])
        if rc == 0:
            _log.info("lock via=cgsession")
            return "Locking the screen."
        rc2, out2 = self._run(["pmset", "displaysleepnow"])
        if rc2 == 0:
            _log.info("lock via=displaysleep")
            return "Locking the screen."
        return f"I couldn't lock the screen: {out2 or 'unknown error'}"


def register_system_toggles(
    registry: ToolRegistry,
    runner: Runner | None = None,
    procs: ProcessManager | None = None,
    installer: Installer | None = None,
) -> SystemToggles:
    """Register the write-side system-control tools into ``registry``.

    Args:
        registry: The tool registry to populate.
        runner: Optional command runner; defaults to subprocess.
        procs: Optional process manager; defaults to subprocess.
        installer: Optional Homebrew installer for the brightness tool.

    Returns:
        The constructed :class:`SystemToggles` instance.
    """
    tools = SystemToggles(runner, procs, installer)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("system toggles registered (volume/brightness/appearance/sleep/wifi/keep-awake/lock)")
    return tools
