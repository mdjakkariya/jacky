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


_APPEARANCE_SET = 'tell application "System Events" to tell appearance preferences'


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
    msg = SystemToggles(runner).set_appearance("toggle")
    assert runner.calls[0][-1] == f"{_APPEARANCE_SET} to set dark mode to not dark mode"
    assert msg == "Now in dark mode."


def test_set_appearance_bad_mode() -> None:
    assert "dark" in SystemToggles(FakeRunner()).set_appearance("rainbow")


def test_set_appearance_failure_is_friendly() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="not authorized")).set_appearance("dark")
    assert "couldn't change the appearance" in msg


def test_set_appearance_dark_readback_fails_still_correct() -> None:
    # SET succeeds, read-back fails -> report the requested mode, not a wrong one.
    runner = SeqRunner([(0, ""), (1, "")])
    assert SystemToggles(runner).set_appearance("dark") == "Now in dark mode."


def test_set_appearance_toggle_readback_fails_is_neutral() -> None:
    runner = SeqRunner([(0, ""), (1, "")])
    assert "switched the appearance" in SystemToggles(runner).set_appearance("toggle")
