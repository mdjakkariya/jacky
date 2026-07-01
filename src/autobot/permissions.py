"""Central macOS permission tracking — one place that knows what Jack needs.

Jack's tools need a few macOS privacy permissions: the **Microphone** (to listen),
**Accessibility** (System Events UI control — hide/minimize/maximize/list), and
**Automation** (Apple Events to other apps — close browser tabs, quit apps, empty
the Trash). Rather than letting a tool fail opaquely deep in AppleScript, we check
the relevant permission *before* running it: if it isn't granted, the tool is
refused with a plain explanation and the right System Settings pane is opened, so
the user can enable it in one place and try again.

Status is checked natively where macOS allows it without prompting:
  * Microphone  — ``AVCaptureDevice.authorizationStatusForMediaType``
  * Accessibility — ``AXIsProcessTrusted()``
  * Automation  — ``AEDeterminePermissionToAutomateTarget(..., askUserIfNeeded=NO)``
Each check is lazy/guarded: off macOS, or without pyobjc, or on any error, it
returns ``UNKNOWN`` and the tool is allowed to try (degrading to the old behavior).
A real denial observed at runtime (a tool hitting ``-1743``) flips the cached state
to ``NEEDED`` so the next call is pre-empted.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable

from autobot.logging_setup import get_logger

_log = get_logger("app")

GRANTED = "granted"
NEEDED = "needed"
UNKNOWN = "unknown"

# Permission keys used as ``ToolSpec.requires`` and in the Settings view.
MICROPHONE = "microphone"
ACCESSIBILITY = "accessibility"
AUTOMATION = "automation"
AUDIO_CAPTURE = "audio_capture"

# Bundle IDs Jack sends Apple Events to (for the native Automation check). Only the
# ones present on the machine matter; missing apps are skipped.
_AUTOMATION_TARGETS = (
    "com.apple.systemevents",
    "com.apple.finder",
    "com.apple.reminders",
    "com.apple.Safari",
    "com.google.Chrome",
    "com.microsoft.edgemac",
)

_PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
_PANE = {
    MICROPHONE: _PRIVACY + "Microphone",
    ACCESSIBILITY: _PRIVACY + "Accessibility",
    AUTOMATION: _PRIVACY + "Automation",
    AUDIO_CAPTURE: _PRIVACY + "ScreenCapture",
}
_LABEL = {
    MICROPHONE: "Microphone",
    ACCESSIBILITY: "Accessibility",
    AUTOMATION: "Automation",
    AUDIO_CAPTURE: "Audio Capture",
}
_WHY = {
    MICROPHONE: "Needed for Jack to hear you.",
    ACCESSIBILITY: "Lets Jack show, hide, minimize, and list your app windows.",
    AUTOMATION: "Lets Jack control apps — close browser tabs, quit apps, empty the Trash.",
    AUDIO_CAPTURE: "Lets Jack capture the other participants' audio during a meeting.",
}

# Runtime overrides learned from actual tool outcomes (e.g. a -1743 denial). Wins
# over a native UNKNOWN, so an observed denial pre-empts the next call.
_observed: dict[str, str] = {}


def _microphone_status() -> str:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
        code = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        return {3: GRANTED, 2: NEEDED, 1: NEEDED}.get(code, UNKNOWN)
    except Exception:
        return UNKNOWN


def _accessibility_status() -> str:
    try:
        from ApplicationServices import AXIsProcessTrusted

        # `True` is authoritative (we're trusted). `False` from the engine sidecar is
        # NOT — the tools act via osascript and macOS attributes trust to Jack.app, a
        # different code identity — so report "unknown" instead of a misleading
        # "needed", and let a real tool outcome (or the user) settle it.
        return GRANTED if AXIsProcessTrusted() else UNKNOWN
    except Exception:
        return UNKNOWN


def _automation_target_status(bundle_id: str) -> str:
    """Native, prompt-free check of whether we may send Apple Events to ``bundle_id``."""
    try:
        from CoreServices import (
            AEDeterminePermissionToAutomateTarget,
            typeWildCard,
        )
        from Foundation import NSAppleEventDescriptor

        target = NSAppleEventDescriptor.descriptorWithBundleIdentifier_(bundle_id)
        status = AEDeterminePermissionToAutomateTarget(
            target.aeDesc(), typeWildCard, typeWildCard, False
        )
        # 0 = allowed; -1743 = denied; -1744 = would prompt (not yet decided).
        if status == 0:
            return GRANTED
        if status == -1743:
            return NEEDED
        return UNKNOWN
    except Exception:
        return UNKNOWN


def _automation_status() -> str:
    """Aggregate Automation across the targets Jack uses.

    Per-target and coarse on purpose: if *any* target is granted we show GRANTED
    (you've allowed Jack to control something — the rest prompt on first use); if
    none are granted but some are denied, NEEDED; otherwise UNKNOWN. The gate's
    per-tool reactive path still surfaces a specific target that's actually blocked.
    """
    seen = {_automation_target_status(bundle) for bundle in _AUTOMATION_TARGETS}
    if GRANTED in seen:
        return GRANTED
    if NEEDED in seen:
        return NEEDED
    return UNKNOWN


_NATIVE = {
    MICROPHONE: _microphone_status,
    ACCESSIBILITY: _accessibility_status,
    AUTOMATION: _automation_status,
}


def status_of(key: str) -> str:
    """Current status of a permission: ``granted`` / ``needed`` / ``unknown``.

    A runtime-observed denial wins over a native ``unknown`` so we don't keep
    retrying a tool we already know is blocked.
    """
    native = _NATIVE.get(key, lambda: UNKNOWN)()
    if native != UNKNOWN:
        return native
    return _observed.get(key, UNKNOWN)


def note_observed(key: str, granted: bool) -> None:
    """Record what a real tool outcome told us about a permission."""
    _observed[key] = GRANTED if granted else NEEDED


def open_pane(key: str) -> bool:
    """Open the System Settings privacy pane for ``key``. Returns whether it ran."""
    url = _PANE.get(key)
    if not url:
        return False
    try:
        subprocess.run(["open", url], capture_output=True, timeout=5, check=False)
        return True
    except Exception:
        _log.warning("couldn't open Settings pane for %s", key)
        return False


def snapshot(keys: Iterable[str] = (MICROPHONE, ACCESSIBILITY, AUTOMATION)) -> list[dict[str, str]]:
    """Status of each permission, for the Settings view / ``/permissions`` endpoint."""
    return [
        {
            "key": key,
            "label": _LABEL.get(key, key),
            "description": _WHY.get(key, ""),
            "status": status_of(key),
        }
        for key in keys
    ]


def needed_message(key: str) -> str:
    """The spoken reply when a tool is blocked by a missing permission."""
    label = _LABEL.get(key, key)
    return (
        f"🔐 I don't have {label} permission yet, so I can't do that just yet. I've "
        f"opened the right Settings pane for you — flip it on for Jack and ask me again!"
    )
