"""Tests for the write-side system controls (helpers + each tool + dispatch)."""

from __future__ import annotations

from autobot.config import Settings
from autobot.core.types import Risk
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry
from autobot.tools.toggles import (
    SystemToggles,
    clamp,
    first_int,
    is_accessibility_error,
    register_system_toggles,
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


def test_sleep_mac_calls_pmset() -> None:
    runner = FakeRunner()
    assert SystemToggles(runner).sleep_mac() == "Going to sleep."
    assert runner.calls[-1] == ["pmset", "sleepnow"]


def test_sleep_mac_failure_is_friendly() -> None:
    msg = SystemToggles(FakeRunner(rc=1, out="denied")).sleep_mac()
    assert "couldn't put the Mac to sleep" in msg


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
    # Second keep-awake will return a new pid via procs.start()
    second_pid = procs._next  # Save expected pid before the second call
    tools.keep_awake(minutes=10)
    # Starting a second keep-awake stops the first.
    assert procs.stopped == [first]
    assert len(procs.started) == 2
    assert tools._awake_pid == second_pid  # new pid tracked, not the stopped one


def test_keep_awake_one_minute_singular() -> None:
    tools = SystemToggles(FakeRunner(), FakeProcs())
    assert "1 minute." in tools.keep_awake(minutes=1)


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


def test_system_toggles_enabled_by_default() -> None:
    assert Settings().allow_system_toggles is True
