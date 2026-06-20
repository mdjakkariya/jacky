"""Tests for the read-only system-status tools (parsers + dispatch)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry
from autobot.tools.system import (
    SystemTools,
    parse_battery,
    parse_disk,
    parse_ssid,
    parse_wifi_device,
    register_system_tools,
)


class FakeRunner:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.rc = rc
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.rc, self.out


# --- battery -------------------------------------------------------------
def test_battery_charging() -> None:
    out = "Now drawing from 'AC Power'\n -InternalBattery-0 82%; charging; 1:12 remaining"
    assert parse_battery(out) == "Battery is at 82% and charging."


def test_battery_on_battery() -> None:
    out = "Now drawing from 'Battery Power'\n -InternalBattery-0 64%; discharging; 3:01 remaining"
    assert parse_battery(out) == "Battery is at 64% on battery power."


def test_battery_charged() -> None:
    out = "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged; 0:00 remaining"
    assert parse_battery(out) == "Battery is at 100%, fully charged."


def test_battery_none() -> None:
    assert "doesn't seem to have a battery" in parse_battery("Now drawing from 'AC Power'")


# --- wifi ----------------------------------------------------------------
def test_parse_wifi_device_finds_the_wifi_interface() -> None:
    out = (
        "Hardware Port: Ethernet\nDevice: en1\nEthernet Address: 1\n\n"
        "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 2\n"
    )
    assert parse_wifi_device(out) == "en0"
    assert parse_wifi_device("no wifi here") is None


def test_parse_ssid() -> None:
    assert parse_ssid("Current Wi-Fi Network: HomeNet") == "HomeNet"
    assert parse_ssid("Current Wi-Fi Network: <redacted>") is None
    assert parse_ssid("You are not associated with an AirPort network.") is None


class WifiRouter:
    """Routes argv to canned outputs so wifi_status can be tested end-to-end."""

    def __init__(self, ssid_out: str, ip_out: tuple[int, str], power_out: str = "On") -> None:
        self._ssid_out = ssid_out
        self._ip_out = ip_out
        self._power_out = power_out

    def __call__(self, args: list[str]) -> tuple[int, str]:
        if "-listallhardwareports" in args:
            return 0, "Hardware Port: Wi-Fi\nDevice: en0\n"
        if "-getairportnetwork" in args:
            return 0, self._ssid_out
        if "getifaddr" in args:
            return self._ip_out
        if "-getairportpower" in args:
            return 0, f"Wi-Fi Power (en0): {self._power_out}"
        return 0, ""


def test_wifi_reports_ssid_when_available() -> None:
    tools = SystemTools(WifiRouter("Current Wi-Fi Network: HomeNet", (1, "")))
    assert tools.wifi_status() == "You're connected to HomeNet."


def test_wifi_connected_even_when_name_is_withheld() -> None:
    # No SSID (redacted), but the interface has an IP -> we ARE connected.
    tools = SystemTools(WifiRouter("Current Wi-Fi Network: <redacted>", (0, "192.168.1.5")))
    assert "connected to Wi-Fi" in tools.wifi_status()


def test_wifi_off() -> None:
    tools = SystemTools(WifiRouter("not associated", (1, ""), power_out="Off"))
    assert tools.wifi_status() == "Wi-Fi is turned off."


def test_wifi_on_but_disconnected() -> None:
    tools = SystemTools(WifiRouter("not associated", (1, ""), power_out="On"))
    assert tools.wifi_status() == "You're not connected to any Wi-Fi network."


# --- disk ----------------------------------------------------------------
def test_disk_parses_free_and_total() -> None:
    out = (
        "Filesystem      Size   Used  Avail Capacity iused ifree %iused  Mounted on\n"
        "/dev/disk3s5   460Gi  300Gi  120Gi    72%   ...   ...   ...   /System/Volumes/Data"
    )
    assert parse_disk(out) == "You have 120 GB free out of 460 GB (72% used)."


def test_disk_garbage_is_handled() -> None:
    assert parse_disk("") == "Couldn't read the disk space."


# --- dispatch / specs ----------------------------------------------------
def test_dispatch_resolves_device_then_queries_network() -> None:
    runner = FakeRunner(out="Current Wi-Fi Network: HomeNet")
    SystemTools(runner).wifi_status()
    # First resolves the Wi-Fi device, then queries the network on it.
    assert runner.calls[0] == ["networksetup", "-listallhardwareports"]
    assert ["networksetup", "-getairportnetwork", "en0"] in runner.calls


def test_disk_falls_back_to_root_when_data_volume_fails() -> None:
    class Seq:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str]) -> tuple[int, str]:
            self.calls.append(args)
            # First call (Data volume) fails, second (root) succeeds.
            if len(self.calls) == 1:
                return 1, "No such file or directory"
            return 0, "FS Size Used Avail Cap\n/ 460Gi 300Gi 120Gi 72% /"

    seq = Seq()
    SystemTools(seq).disk_space()
    assert seq.calls[0][-1] == "/System/Volumes/Data"
    assert seq.calls[1][-1] == "/"


def test_all_tools_are_read_only_and_registered() -> None:
    registry = ToolRegistry()
    register_system_tools(registry, FakeRunner())
    for name in ("battery_status", "wifi_status", "disk_space"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.risk is Risk.READ_ONLY
