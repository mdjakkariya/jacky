"""Read-only system-status tools — the everyday "how's my Mac?" questions.

Battery charge, Wi-Fi network, and free disk space, answered in a short spoken
sentence. All are ``READ_ONLY``: they only query the system (``pmset`` /
``networksetup`` / ``df``), never change it, so the permission gate runs them
unprompted and audits them like anything else.

A ``Runner`` is injected so the parsing logic is unit-tested against canned
command output, with no real processes and no dependence on the host machine.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("system")

RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]

# The user's data lives on the Data volume on modern macOS; fall back to root.
_DATA_VOLUME = "/System/Volumes/Data"
# Wi-Fi is en0 on virtually all Macs.
_WIFI_IF = "en0"


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


def parse_battery(out: str) -> str:
    """Turn ``pmset -g batt`` output into a spoken battery summary."""
    match = re.search(r"(\d+)%", out)
    if not match:
        return "This Mac doesn't seem to have a battery."
    pct = match.group(1)
    low = out.lower()
    # Check 'discharging' before 'charging' — the latter is a substring of it.
    if "discharging" in low or "battery power" in low:
        return f"Battery is at {pct}% on battery power."
    if "charged" in low:
        state = "fully charged" if pct == "100" else "charged"
        return f"Battery is at {pct}%, {state}."
    if "charging" in low:
        return f"Battery is at {pct}% and charging."
    if "ac power" in low:
        return f"Battery is at {pct}% and plugged in."
    return f"Battery is at {pct}%."


def parse_wifi_device(out: str) -> str | None:
    """Find the Wi-Fi interface (e.g. 'en0') from ``-listallhardwareports``.

    Hardcoding en0 is wrong on Macs where Wi-Fi is en1 (Ethernet/Thunderbolt
    takes en0), which makes status checks query the wrong interface.
    """
    match = re.search(r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*\nDevice:\s*(\S+)", out)
    return match.group(1) if match else None


def parse_ssid(out: str) -> str | None:
    """Extract the SSID from ``-getairportnetwork`` output, or ``None``.

    Returns ``None`` when not associated or when macOS withholds the name
    (recent versions redact it without Location permission).
    """
    match = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
    if not match:
        return None
    ssid = match.group(1).strip()
    if not ssid or ssid.lower() in {"<redacted>", "redacted"}:
        return None
    return ssid


def _humanize_size(token: str) -> str:
    """'460Gi' -> '460 GB', '512Mi' -> '512 MB' for nicer speech."""
    repl = {"Gi": " GB", "Mi": " MB", "Ti": " TB", "Ki": " KB"}
    for suffix, spoken in repl.items():
        if token.endswith(suffix):
            return token[: -len(suffix)] + spoken
    return token


def parse_disk(out: str) -> str:
    """Turn ``df -h`` output into a spoken free-space summary.

    Columns (macOS): Filesystem Size Used Avail Capacity ... Mounted-on.
    """
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return "Couldn't read the disk space."
    fields = lines[1].split()
    if len(fields) < 5:
        return "Couldn't read the disk space."
    size, avail, capacity = fields[1], fields[3], fields[4]
    return f"You have {_humanize_size(avail)} free out of {_humanize_size(size)} ({capacity} used)."


class SystemTools:
    """Read-only system status queries exposed as tools."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _subprocess_runner

    def battery_status(self) -> str:
        """Report battery charge and whether it's charging/plugged in."""
        rc, out = self._run(["pmset", "-g", "batt"])
        if rc != 0:
            return "Couldn't read the battery status."
        return parse_battery(out)

    def _wifi_device(self) -> str:
        """Resolve the Wi-Fi interface dynamically, falling back to en0."""
        rc, out = self._run(["networksetup", "-listallhardwareports"])
        if rc == 0:
            dev = parse_wifi_device(out)
            if dev:
                return dev
        return _WIFI_IF

    def wifi_status(self) -> str:
        """Report the Wi-Fi network, or connection status if the name is withheld."""
        dev = self._wifi_device()
        _rc, net = self._run(["networksetup", "-getairportnetwork", dev])
        ssid = parse_ssid(net)
        if ssid:
            return f"You're connected to {ssid}."
        # No SSID — either truly disconnected, or macOS is withholding the name.
        # An assigned IP on the Wi-Fi interface means we're actually connected.
        ip_rc, ip = self._run(["ipconfig", "getifaddr", dev])
        if ip_rc == 0 and ip.strip():
            return "You're connected to Wi-Fi (the network name needs Location permission to read)."
        power_rc, power = self._run(["networksetup", "-getairportpower", dev])
        if power_rc == 0 and "off" in power.lower():
            return "Wi-Fi is turned off."
        return "You're not connected to any Wi-Fi network."

    def disk_space(self) -> str:
        """Report free disk space on the main volume."""
        rc, out = self._run(["df", "-h", _DATA_VOLUME])
        if rc != 0:
            rc, out = self._run(["df", "-h", "/"])
        if rc != 0:
            return "Couldn't read the disk space."
        return parse_disk(out)

    def specs(self) -> list[ToolSpec]:
        """Return the read-only tool specs."""
        no_params = {"type": "object", "properties": {}, "required": []}
        return [
            ToolSpec(
                name="battery_status",
                description="Check the Mac's battery level and charging state.",
                parameters=no_params,
                handler=self.battery_status,
                risk=Risk.READ_ONLY,
                core=True,
            ),
            ToolSpec(
                name="wifi_status",
                description="Check which Wi-Fi network the Mac is connected to.",
                parameters=no_params,
                handler=self.wifi_status,
                risk=Risk.READ_ONLY,
                core=True,
            ),
            ToolSpec(
                name="disk_space",
                description="Check how much free disk storage is left.",
                parameters=no_params,
                handler=self.disk_space,
                risk=Risk.READ_ONLY,
                core=True,
            ),
        ]


def register_system_tools(registry: ToolRegistry, runner: Runner | None = None) -> SystemTools:
    """Register the read-only system-status tools into ``registry``."""
    tools = SystemTools(runner)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("system-info tools registered (battery/wifi/disk)")
    return tools
