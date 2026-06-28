# System Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a write-side `tools/toggles.py` giving Jack seven on-device macOS controls — `set_volume`, `set_brightness`, `set_appearance`, `sleep_mac`, `set_wifi`, `keep_awake`, `lock_screen` — all routed through the registry + permission gate.

**Architecture:** A `SystemToggles` class mirrors the existing read-only `tools/system.py`: an injected `Runner` for one-shot commands and an injected `ProcessManager` for `caffeinate`'s background process, plus pure helpers, a `specs()` list of `ToolSpec`, and a `register_system_toggles()` function wired in `app.py::build()` behind a new `allow_system_toggles` flag. Fragile paths (brightness, Wi-Fi, lock) degrade gracefully at runtime and never run `sudo`.

**Tech Stack:** Python 3.11+, stdlib `subprocess`/`os`/`signal`/`re`, `osascript` / `pmset` / `networksetup` / `caffeinate`, pytest.

**Design reference:** [docs/plans/autobot_system_toggles_plan.md](../../plans/autobot_system_toggles_plan.md) (issue #4).

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- Full type hints; **mypy strict must stay green**.
- Google-style docstrings on public modules/classes/functions (ruff `D` rules); **tests are exempt**.
- Line length 100; **never hand-format — run `make format`** (ruff owns formatting + import order).
- Tools **return strings and never raise** out of their handler; errors become friendly strings.
- **On-device only**; **never run `sudo`** or escalate privileges.
- Import heavy runtimes **lazily** (inside functions/methods) — keep module import fast.
- Per-component logger: `from autobot.logging_setup import get_logger` → `_log = get_logger("toggles")`; log at seams, `key=value`, `%`-style args (not f-strings).
- Commits: **Conventional Commits**. **Do NOT add a `Co-Authored-By` trailer** (repo rule).
- All seven tools are `Risk.WRITE`; only `set_appearance` sets `requires=AUTOMATION`.
- Verify with `make check` (ruff, ruff-format, mypy strict, pytest) before declaring done.

---

### Task 1: Module scaffold — `Runner`, `ProcessManager`, pure helpers, `SystemToggles.__init__`

**Files:**
- Create: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Consumes: `autobot.tools.registry.ToolRegistry`/`ToolSpec`, `autobot.core.types.Risk`, `autobot.permissions.AUTOMATION`, `autobot.tools.system.parse_wifi_device`.
- Produces:
  - `RunResult = tuple[int, str]`, `Runner = Callable[[list[str]], RunResult]`
  - `clamp(level: int) -> int`
  - `first_int(out: str) -> int | None`
  - `is_accessibility_error(out: str) -> bool`
  - `class ProcessManager(Protocol)` with `start(argv: list[str]) -> int` and `stop(pid: int) -> None`
  - `class SystemToggles` with `__init__(self, runner: Runner | None = None, procs: ProcessManager | None = None)`; attributes `self._run`, `self._procs`, `self._awake_pid: int | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_toggles.py
"""Tests for the write-side system controls (helpers + each tool + dispatch)."""

from __future__ import annotations

from autobot.tools.toggles import (
    SystemToggles,
    clamp,
    first_int,
    is_accessibility_error,
)


class FakeRunner:
    """Records calls; returns one fixed (rc, out) for every command."""

    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.rc = rc
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.rc, self.out


class SeqRunner:
    """Returns queued (rc, out) responses in order; records calls."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self._responses.pop(0) if self._responses else (0, "")


class FakeProcs:
    """Fake ProcessManager: records started argvs and stopped pids."""

    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.stopped: list[int] = []
        self._next = 1000

    def start(self, argv: list[str]) -> int:
        self.started.append(argv)
        pid = self._next
        self._next += 1
        return pid

    def stop(self, pid: int) -> None:
        self.stopped.append(pid)


def test_clamp_bounds() -> None:
    assert clamp(130) == 100
    assert clamp(-5) == 0
    assert clamp(42) == 42


def test_first_int_extracts_number() -> None:
    assert first_int("30\n") == 30
    assert first_int("Wi-Fi Power (en0): On") == 0  # the 0 in en0
    assert first_int("nothing here") is None


def test_is_accessibility_error() -> None:
    assert is_accessibility_error("System Events got an error: ... (-1719)")
    assert is_accessibility_error("not allowed assistive access")
    assert not is_accessibility_error("some other error")


def test_constructs_with_fakes() -> None:
    tools = SystemToggles(FakeRunner(), FakeProcs())
    assert tools._awake_pid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autobot.tools.toggles'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/autobot/tools/toggles.py
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
    """Clamp a percentage to the 0–100 range."""
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

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class SystemToggles:
    """Write-side macOS controls exposed as tools."""

    def __init__(self, runner: Runner | None = None, procs: ProcessManager | None = None) -> None:
        """Store the injected command runner and process manager."""
        self._run = runner or _subprocess_runner
        self._procs = procs or _SubprocessManager()
        self._awake_pid: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): module scaffold + pure helpers for system controls (#4)"
```

---

### Task 2: `set_volume` — absolute / up / down / mute / unmute

**Files:**
- Modify: `src/autobot/tools/toggles.py` (add method to `SystemToggles`)
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.set_volume(self, level: int | None = None, action: str | None = None) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
def test_set_volume_absolute() -> None:
    runner = FakeRunner()
    msg = SystemToggles(runner).set_volume(level=30)
    assert msg == "Volume set to 30%."
    assert runner.calls[-1] == ["osascript", "-e", "set volume output volume 30"]


def test_set_volume_clamps() -> None:
    runner = FakeRunner()
    assert SystemToggles(runner).set_volume(level=130) == "Volume set to 100%."


def test_set_volume_up_reads_then_sets() -> None:
    # First call reads current (45), second sets 55.
    runner = SeqRunner([(0, "45"), (0, "")])
    msg = SystemToggles(runner).set_volume(action="up")
    assert msg == "Volume set to 55%."
    assert runner.calls[0] == ["osascript", "-e", "output volume of (get volume settings)"]
    assert runner.calls[1] == ["osascript", "-e", "set volume output volume 55"]


def test_set_volume_down_clamps_at_zero() -> None:
    runner = SeqRunner([(0, "5"), (0, "")])
    assert SystemToggles(runner).set_volume(action="down") == "Volume set to 0%."


def test_set_volume_mute_unmute() -> None:
    runner = FakeRunner()
    assert SystemToggles(runner).set_volume(action="mute") == "Muted."
    assert runner.calls[-1] == ["osascript", "-e", "set volume output muted true"]
    assert SystemToggles(runner).set_volume(action="unmute") == "Unmuted."
    assert runner.calls[-1] == ["osascript", "-e", "set volume output muted false"]


def test_set_volume_no_args_asks() -> None:
    assert "Tell me" in SystemToggles(FakeRunner()).set_volume()


def test_set_volume_failure_is_friendly() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="boom")).set_volume(level=20)
    assert "couldn't set the volume" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k set_volume -v`
Expected: FAIL — `AttributeError: 'SystemToggles' object has no attribute 'set_volume'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
    def set_volume(self, level: int | None = None, action: str | None = None) -> str:
        """Set the system volume (0–100) or mute/unmute/nudge it up/down."""
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
            return "Tell me a level (0–100), or whether to turn it up, down, or mute."
        level = clamp(level)
        rc, out = self._run(["osascript", "-e", f"set volume output volume {level}"])
        if rc != 0:
            return f"I couldn't set the volume: {out or 'unknown error'}"
        _log.info("volume set to=%d", level)
        return f"Volume set to {level}%."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k set_volume -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): set_volume (absolute/up/down/mute) (#4)"
```

---

### Task 3: `set_brightness` — binary (absolute) → AppleScript (relative) → friendly message

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.set_brightness(self, level: int | None = None, action: str | None = None) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
def test_set_brightness_absolute_uses_binary() -> None:
    runner = FakeRunner()
    msg = SystemToggles(runner).set_brightness(level=40)
    assert msg == "Brightness set to 40%."
    assert runner.calls[-1] == ["brightness", "0.4"]


def test_set_brightness_binary_missing_gives_setup_message() -> None:
    # rc 127 == command not found.
    msg = SystemToggles(FakeRunner(rc=127, out="command not found")).set_brightness(level=40)
    assert "brew install brightness" in msg


def test_set_brightness_up_uses_key_code() -> None:
    runner = FakeRunner()
    msg = SystemToggles(runner).set_brightness(action="up")
    assert msg == "Brightness turned up."
    assert runner.calls[-1] == [
        "osascript",
        "-e",
        'tell application "System Events" to key code 144',
    ]


def test_set_brightness_down_uses_key_code_145() -> None:
    runner = FakeRunner()
    SystemToggles(runner).set_brightness(action="down")
    assert runner.calls[-1][-1] == 'tell application "System Events" to key code 145'


def test_set_brightness_accessibility_blocked() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="(-1719)")).set_brightness(action="up")
    assert "Accessibility" in msg


def test_set_brightness_no_args_asks() -> None:
    assert "Tell me" in SystemToggles(FakeRunner()).set_brightness()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k set_brightness -v`
Expected: FAIL — `AttributeError: ... 'set_brightness'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
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
            return "Tell me a level (0–100), or whether to make it brighter or dimmer."
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k set_brightness -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): set_brightness with graceful degradation (#4)"
```

---

### Task 4: `set_appearance` — dark / light / toggle (reads back result)

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.set_appearance(self, mode: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
_APPEARANCE_SET = "tell application \"System Events\" to tell appearance preferences"


def test_set_appearance_dark() -> None:
    # Set call succeeds, then read-back returns "true".
    runner = SeqRunner([(0, ""), (0, "true")])
    msg = SystemToggles(runner).set_appearance("dark")
    assert msg == "Now in dark mode."
    assert runner.calls[0][-1] == f"{_APPEARANCE_SET} to set dark mode to true"


def test_set_appearance_light() -> None:
    runner = SeqRunner([(0, ""), (0, "false")])
    msg = SystemToggles(runner).set_appearance("light")
    assert msg == "Now in light mode."
    assert runner.calls[0][-1] == f"{_APPEARANCE_SET} to set dark mode to false"


def test_set_appearance_toggle() -> None:
    runner = SeqRunner([(0, ""), (0, "true")])
    SystemToggles(runner).set_appearance("toggle")
    assert runner.calls[0][-1] == f"{_APPEARANCE_SET} to set dark mode to not dark mode"


def test_set_appearance_bad_mode() -> None:
    assert "dark" in SystemToggles(FakeRunner()).set_appearance("rainbow")


def test_set_appearance_failure_is_friendly() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="not authorized")).set_appearance("dark")
    assert "couldn't change the appearance" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k set_appearance -v`
Expected: FAIL — `AttributeError: ... 'set_appearance'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
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
        _rc, now = self._run(["osascript", "-e", f"{base} to return dark mode"])
        is_dark = now.strip().lower() == "true"
        _log.info("appearance dark=%s", is_dark)
        return "Now in dark mode." if is_dark else "Now in light mode."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k set_appearance -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): set_appearance dark/light/toggle (#4)"
```

---

### Task 5: `sleep_mac` — `pmset sleepnow`

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.sleep_mac(self) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
def test_sleep_mac_calls_pmset() -> None:
    runner = FakeRunner()
    assert SystemToggles(runner).sleep_mac() == "Going to sleep."
    assert runner.calls[-1] == ["pmset", "sleepnow"]


def test_sleep_mac_failure_is_friendly() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="denied")).sleep_mac()
    assert "couldn't put the Mac to sleep" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k sleep_mac -v`
Expected: FAIL — `AttributeError: ... 'sleep_mac'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
    def sleep_mac(self) -> str:
        """Put the Mac to sleep immediately (reversible — just wake it)."""
        rc, out = self._run(["pmset", "sleepnow"])
        if rc != 0:
            return f"I couldn't put the Mac to sleep: {out or 'unknown error'}"
        _log.info("sleeping")
        return "Going to sleep."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k sleep_mac -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): sleep_mac via pmset (#4)"
```

---

### Task 6: `set_wifi` — on / off / toggle (never sudo)

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.set_wifi(self, state: str) -> str` (uses a private `_wifi_device(self) -> str`)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
class WifiRunner:
    """Resolves the device, optionally reports power, records set calls."""

    def __init__(self, power: str = "On", set_result: tuple[int, str] = (0, "")) -> None:
        self._power = power
        self._set_result = set_result
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if "-listallhardwareports" in args:
            return 0, "Hardware Port: Wi-Fi\nDevice: en0\n"
        if "-getairportpower" in args:
            return 0, f"Wi-Fi Power (en0): {self._power}"
        if "-setairportpower" in args:
            return self._set_result
        return 0, ""


def test_set_wifi_on() -> None:
    runner = WifiRunner()
    assert SystemToggles(runner).set_wifi("on") == "Wi-Fi turned on."
    assert ["networksetup", "-setairportpower", "en0", "on"] in runner.calls


def test_set_wifi_toggle_when_on_turns_off() -> None:
    runner = WifiRunner(power="On")
    assert SystemToggles(runner).set_wifi("toggle") == "Wi-Fi turned off."
    assert ["networksetup", "-setairportpower", "en0", "off"] in runner.calls


def test_set_wifi_never_uses_sudo() -> None:
    runner = WifiRunner()
    SystemToggles(runner).set_wifi("off")
    assert all("sudo" not in arg for call in runner.calls for arg in call)


def test_set_wifi_admin_required_is_friendly() -> None:
    runner = WifiRunner(set_result=(1, "You must have administrator access"))
    msg = SystemToggles(runner).set_wifi("off")
    assert "admin" in msg.lower()


def test_set_wifi_bad_state() -> None:
    assert "on" in SystemToggles(WifiRunner()).set_wifi("sideways")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k set_wifi -v`
Expected: FAIL — `AttributeError: ... 'set_wifi'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k set_wifi -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): set_wifi on/off/toggle without sudo (#4)"
```

---

### Task 7: `keep_awake` — `caffeinate` via the injected ProcessManager

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.keep_awake(self, minutes: int | None = None, off: bool = False) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
def test_keep_awake_timed() -> None:
    procs = FakeProcs()
    tools = SystemToggles(FakeRunner(), procs)
    msg = tools.keep_awake(minutes=30)
    assert "30 minutes" in msg
    assert procs.started[-1] == ["caffeinate", "-dimsu", "-t", "1800"]
    assert tools._awake_pid is not None


def test_keep_awake_indefinite() -> None:
    procs = FakeProcs()
    tools = SystemToggles(FakeRunner(), procs)
    msg = tools.keep_awake()
    assert "until you tell me to stop" in msg
    assert procs.started[-1] == ["caffeinate", "-dimsu"]


def test_keep_awake_off_stops_tracked_pid() -> None:
    procs = FakeProcs()
    tools = SystemToggles(FakeRunner(), procs)
    tools.keep_awake()
    pid = tools._awake_pid
    msg = tools.keep_awake(off=True)
    assert "sleep normally" in msg
    assert procs.stopped == [pid]
    assert tools._awake_pid is None


def test_keep_awake_off_when_nothing_active() -> None:
    tools = SystemToggles(FakeRunner(), FakeProcs())
    assert "wasn't being kept awake" in tools.keep_awake(off=True)


def test_keep_awake_replaces_previous_without_leaking() -> None:
    procs = FakeProcs()
    tools = SystemToggles(FakeRunner(), procs)
    tools.keep_awake()
    first = tools._awake_pid
    tools.keep_awake(minutes=10)
    # Starting a second keep-awake stops the first.
    assert procs.stopped == [first]
    assert len(procs.started) == 2


def test_keep_awake_one_minute_singular() -> None:
    tools = SystemToggles(FakeRunner(), FakeProcs())
    assert "1 minute." in tools.keep_awake(minutes=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k keep_awake -v`
Expected: FAIL — `AttributeError: ... 'keep_awake'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k keep_awake -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): keep_awake via caffeinate + ProcessManager (#4)"
```

---

### Task 8: `lock_screen` — CGSession → keystroke fallback

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Produces: `SystemToggles.lock_screen(self) -> str`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
def test_lock_screen_uses_cgsession_when_present() -> None:
    runner = FakeRunner()  # rc 0 == CGSession path works
    assert SystemToggles(runner).lock_screen() == "Locking the screen."
    assert runner.calls[-1][-1] == "-suspend"


def test_lock_screen_falls_back_to_keystroke() -> None:
    # CGSession missing (rc 127), keystroke succeeds (rc 0).
    runner = SeqRunner([(127, "command not found"), (0, "")])
    assert SystemToggles(runner).lock_screen() == "Locking the screen."
    assert runner.calls[1][0] == "osascript"
    assert "control down" in runner.calls[1][-1]


def test_lock_screen_keystroke_blocked_is_friendly() -> None:
    runner = SeqRunner([(127, "no path"), (1, "(-1719)")])
    assert "Accessibility" in SystemToggles(runner).lock_screen()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k lock_screen -v`
Expected: FAIL — `AttributeError: ... 'lock_screen'`

- [ ] **Step 3: Write minimal implementation**

Add to the `SystemToggles` class:

```python
    def lock_screen(self) -> str:
        """Lock the screen; fall back to a keystroke where CGSession is gone."""
        rc, _out = self._run([_CGSESSION, "-suspend"])
        if rc == 0:
            _log.info("lock via=cgsession")
            return "Locking the screen."
        keystroke = 'tell application "System Events" to keystroke "q" using {control down, command down}'
        rc2, out2 = self._run(["osascript", "-e", keystroke])
        if rc2 == 0:
            _log.info("lock via=keystroke")
            return "Locking the screen."
        if is_accessibility_error(out2):
            return (
                "I need Accessibility access to lock the screen. Enable Jack under System "
                "Settings → Privacy & Security → Accessibility."
            )
        return f"I couldn't lock the screen: {out2 or 'unknown error'}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -k lock_screen -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): lock_screen with keystroke fallback (#4)"
```

---

### Task 9: `specs()` + `register_system_toggles()` — register all seven

**Files:**
- Modify: `src/autobot/tools/toggles.py`
- Test: `tests/unit/test_toggles.py`

**Interfaces:**
- Consumes: `SystemToggles` methods from Tasks 2–8; `ToolSpec`, `Risk`, `AUTOMATION`.
- Produces:
  - `SystemToggles.specs(self) -> list[ToolSpec]`
  - `register_system_toggles(registry: ToolRegistry, runner: Runner | None = None, procs: ProcessManager | None = None) -> SystemToggles`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
from autobot.core.types import Risk
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry
from autobot.tools.toggles import register_system_toggles

_TOGGLE_NAMES = (
    "set_volume",
    "set_brightness",
    "set_appearance",
    "sleep_mac",
    "set_wifi",
    "keep_awake",
    "lock_screen",
)


def test_all_toggles_registered_as_write() -> None:
    registry = ToolRegistry()
    register_system_toggles(registry, FakeRunner(), FakeProcs())
    for name in _TOGGLE_NAMES:
        spec = registry.get(name)
        assert spec is not None, name
        assert spec.risk is Risk.WRITE, name


def test_only_appearance_requires_automation() -> None:
    registry = ToolRegistry()
    register_system_toggles(registry, FakeRunner(), FakeProcs())
    appearance = registry.get("set_appearance")
    assert appearance is not None
    assert appearance.requires == AUTOMATION
    for name in ("set_volume", "set_brightness", "set_wifi", "lock_screen", "sleep_mac"):
        spec = registry.get(name)
        assert spec is not None, name
        assert spec.requires is None, name


def test_dispatch_runs_through_registry() -> None:
    registry = ToolRegistry()
    register_system_toggles(registry, FakeRunner(), FakeProcs())
    result = registry.dispatch("set_volume", {"level": 25})
    assert result.ok
    assert "25%" in result.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k "registered or requires or dispatch_runs" -v`
Expected: FAIL — `ImportError: cannot import name 'register_system_toggles'`

- [ ] **Step 3: Write minimal implementation**

Add the `specs()` method to `SystemToggles`, then the module-level `register_system_toggles`:

```python
    def specs(self) -> list[ToolSpec]:
        """Return the write-side control tool specs."""
        no_params: dict[str, object] = {"type": "object", "properties": {}, "required": []}
        return [
            ToolSpec(
                name="set_volume",
                description=(
                    "Change the Mac's output volume. Cues: 'set volume to 30', 'turn it "
                    "up/down', 'louder/quieter', 'mute', 'unmute'. Pass `level` (0–100) for an "
                    "exact level, or `action` = up | down | mute | unmute."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "description": "Exact volume, 0–100."},
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
                    "'dimmer'. Pass `level` (0–100) for an exact level (needs the 'brightness' "
                    "tool installed), or `action` = up | down to nudge it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "description": "Exact brightness, 0–100."},
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


def register_system_toggles(
    registry: ToolRegistry,
    runner: Runner | None = None,
    procs: ProcessManager | None = None,
) -> SystemToggles:
    """Register the write-side system-control tools into ``registry``."""
    tools = SystemToggles(runner, procs)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("system toggles registered (volume/brightness/appearance/sleep/wifi/keep-awake/lock)")
    return tools
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_toggles.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/toggles.py tests/unit/test_toggles.py
git commit -m "feat(toggles): specs + register_system_toggles (#4)"
```

---

### Task 10: Wire into config + composition root

**Files:**
- Modify: `src/autobot/config.py:187` (add the flag after `allow_file_io`)
- Modify: `src/autobot/app.py:395` (register after the `allow_system_info` block)
- Test: `tests/unit/test_toggles.py` (config default)

**Interfaces:**
- Consumes: `register_system_toggles` from Task 9; `Settings`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_toggles.py
from autobot.config import Settings


def test_system_toggles_enabled_by_default() -> None:
    assert Settings().allow_system_toggles is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_toggles.py -k enabled_by_default -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'allow_system_toggles'`

- [ ] **Step 3: Write minimal implementation**

In `src/autobot/config.py`, add the flag in the `--- capabilities ---` block, right after `allow_file_io: bool = True` (currently line 187):

```python
    allow_file_io: bool = True
    allow_system_toggles: bool = True
```

In `src/autobot/app.py`, add this block immediately after the `allow_system_info` block (after current line 395, before the `allow_file_search` block):

```python
    if settings.allow_system_toggles:
        # Write-side system controls (volume/brightness/appearance/sleep/wifi/
        # keep-awake/lock) — audited WRITEs through the gate; no sudo, ever.
        from autobot.tools.toggles import register_system_toggles

        register_system_toggles(registry)
        log.info("system toggles ENABLED (volume/brightness/appearance/sleep/wifi/keep-awake/lock)")
```

- [ ] **Step 4: Run test + full check**

Run: `uv run pytest tests/unit/test_toggles.py -k enabled_by_default -v`
Expected: PASS

Then the whole suite + linters:

Run: `make check`
Expected: ruff, ruff-format, mypy (strict), and pytest all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/config.py src/autobot/app.py tests/unit/test_toggles.py
git commit -m "feat(toggles): enable system controls in config + composition root (#4)"
```

---

### Task 11: Manual smoke test on a real Mac (no code)

**Files:** none (verification only).

- [ ] **Step 1: Run Jack and exercise each tool by chat**

Run: `make run` (Ollama must be running). Then in chat, try:
- "set volume to 20" → "Volume set to 20%."; "turn it up" → 30%; "mute" / "unmute".
- "set brightness to 50" → exact % if `brightness` is installed, else the install hint; "make it dimmer" → may prompt Accessibility the first time.
- "switch to dark mode" / "go light" → appearance flips (Automation prompt the first time).
- "turn off Wi-Fi" / "turn it back on" → flips, or the admin-rights message on a locked-down Mac.
- "keep my Mac awake for 2 minutes" → confirmation; verify with `pmset -g assertions | grep -i caffeinate`; "stop keeping awake".
- "lock my screen" → screen locks.
- "go to sleep" → Mac sleeps (do this last).

- [ ] **Step 2: Confirm the audit log + component log**

Run: `make logs-grep C=toggles`
Expected: one INFO line per action (e.g. `volume set to=20`, `appearance dark=True`, `wifi state=off`, `keep_awake minutes=2 …`, `lock via=…`, `sleeping`). Confirm each call also appears in the gate's audit log as an allowed WRITE.

---

## Self-Review

**1. Spec coverage** (against [the design doc](../../plans/autobot_system_toggles_plan.md)):
- §3.1 seven tools → Tasks 2–8; specs/risk/requires → Task 9. ✅
- §3.2 commands (volume/appearance/sleep/brightness/wifi/lock) → encoded in Tasks 2–8. ✅
- §3.3 `ProcessManager` + in-memory pid + replace-previous + off → Task 1 (Protocol) + Task 7. ✅
- §3.4 graceful degradation + `requires` unset on brightness/wifi/lock, `automation` on appearance, never sudo → Tasks 3, 6, 8, 9. ✅
- §3.5 wiring (config flag default True, app.py block, AUTOMATION import, `toggles` logger) → Tasks 1, 9, 10. ✅
- §4 all WRITE, no confirmation (no `confirm_prompt` set anywhere) → Task 9. ✅
- §5 testing (helpers, every tool, registration, fakes) → Tasks 1–10. ✅

**2. Placeholder scan:** No TBD/TODO; every code and test step is complete; no "similar to Task N". ✅

**3. Type consistency:** `Runner`/`RunResult`, `ProcessManager.start/stop`, `clamp`, `first_int`, `is_accessibility_error`, `SystemToggles(runner, procs)`, `_awake_pid`, and `register_system_toggles(registry, runner, procs)` are used identically across tasks. Tool handlers are bound methods with signatures matching their `parameters` schemas. ✅

No gaps found.
