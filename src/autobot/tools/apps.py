"""App-control tools — drive the lifecycle of macOS apps by voice.

Jack can open, focus, hide, minimize, maximize, quit, and list applications, and
(carefully) uninstall one. Everything routes through the same registry +
permission gate as the filesystem tools, so each call is risk-classified and
audited: the lifecycle actions are ``WRITE`` (run unprompted, but logged),
listing is ``READ_ONLY``, and **uninstall is ``DESTRUCTIVE``** so the gate asks
for confirmation first.

Implementation is macOS-native: ``open`` to launch and ``osascript``
(AppleScript) for the rest. The app name is always passed to ``osascript`` as a
run argument (``on run argv``), never interpolated into the script body, so a
spoken name can't inject AppleScript. Controlling other apps uses macOS
Automation/Accessibility, so the first run will prompt the user to grant
permission (System Settings → Privacy & Security).

A ``Runner`` is injected so the pure command-building logic is unit-tested
without spawning any process or touching the OS.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.permissions import AUTOMATION
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("apps")

# A runner takes an argv list and returns (return_code, combined_output).
RunResult = tuple[int, str]
Runner = Callable[[list[str]], RunResult]

# AppleScript snippets. The app name arrives as ``item 1 of argv`` (data, not
# code), so it is never spliced into the script text.
#
_QUIT = "on run argv\ntell application (item 1 of argv) to quit\nend run"
_HIDE = (
    'on run argv\ntell application "System Events" to set visible of '
    "(first application process whose name is (item 1 of argv)) to false\nend run"
)
# Minimize/maximize act on the TARGET app's own window via Accessibility
# attributes — never global keystrokes. A keystroke goes to whatever app is
# frontmost at that instant, which could be the terminal (it was), so it must be
# avoided. Setting AXMinimized/AXFullScreen on the named process's window 1 can
# only ever affect that app, with no dependence on focus. Guarded so an app with
# no window fails cleanly instead of erroring on "window 1".
_MINIMIZE = (
    "on run argv\n"
    'tell application "System Events"\n'
    "set procs to (application processes whose name is (item 1 of argv))\n"
    'if procs is {} then return "not running"\n'
    "tell item 1 of procs\n"
    'if (count of windows) is 0 then return "no window"\n'
    'set value of attribute "AXMinimized" of window 1 to true\n'
    "end tell\n"
    "end tell\nend run"
)
_MAXIMIZE = (
    # Restore the window first (reopen un-minimizes), then full-screen the target
    # app's own window — no keystroke, so it can't land on the wrong app.
    "on run argv\n"
    "tell application (item 1 of argv) to reopen\n"
    "delay 0.3\n"
    'tell application "System Events"\n'
    "set procs to (application processes whose name is (item 1 of argv))\n"
    'if procs is {} then return "not running"\n'
    "tell item 1 of procs\n"
    'if (count of windows) is 0 then return "no window"\n'
    'set value of attribute "AXFullScreen" of window 1 to true\n'
    "end tell\n"
    "end tell\nend run"
)
_LIST = (
    'tell application "System Events" to get name of '
    "(every application process whose background only is false)"
)
_UNINSTALL = (
    'on run argv\ntell application "Finder" to delete '
    '(POSIX file ("/Applications/" & (item 1 of argv) & ".app"))\nend run'
)
# Browsers we can close tabs in via AppleScript (Chromium family + Safari).
_TAB_BROWSERS = ("Safari", "Google Chrome", "Microsoft Edge")


def _close_tab_script(browser: str) -> str:
    """AppleScript that closes tabs whose URL matches argv[1], in ``browser``.

    The app name is baked in as a *literal* (not a run-arg): ``tell application
    <variable>`` doesn't load the target's scripting terminology, so ``tabs``/``URL``
    fail to resolve (Chrome error -1700). The query stays a run-arg so a spoken site
    name can't inject script. Guarded by ``is running`` so it never launches a closed
    browser; iterate windows by index and copy tabs into a list (inline
    ``tab i of window wi`` won't compile, -2741), closing matches in reverse.
    ``browser`` is one of our fixed constants, never user text.
    """
    return (
        "on run argv\n"
        "set q to item 1 of argv\n"
        "set n to 0\n"
        f'if application "{browser}" is running then\n'
        f'tell application "{browser}"\n'
        "repeat with wi from 1 to (count windows)\n"
        "set theTabs to tabs of window wi\n"
        "repeat with i from (count theTabs) to 1 by -1\n"
        "set t to item i of theTabs\n"
        "if (URL of t) contains q then\n"
        "close t\n"
        "set n to n + 1\n"
        "end if\n"
        "end repeat\n"
        "end repeat\n"
        "end tell\n"
        "end if\n"
        "return n\n"
        "end run"
    )


# Spoken when macOS blocks control because the host app lacks Accessibility
# permission. Jack can't flip this switch itself — only the user can — so it asks.
_PERMISSION_HINT = (
    "I need permission to control your apps. macOS should be asking — please allow "
    "Accessibility access for the app running me in System Settings, under Privacy "
    "and Security, Accessibility. I can't turn that on myself; once you do, just "
    "ask me again."
)


def _is_not_installed(output: str) -> bool:
    """True when `open -a` couldn't find the app (i.e. it isn't installed)."""
    low = output.lower()
    return "unable to find application" in low or "no application found" in low


def _is_permission_error(output: str) -> bool:
    """True when an osascript failure is a macOS Automation/Accessibility denial."""
    low = output.lower()
    return any(
        marker in low
        for marker in (
            "assistive access",  # System Events not allowed assistive access
            "not allowed",
            "not authorized",  # "Not authorized to send Apple events to <app>"
            "apple events",
            "-1719",  # errAEEventNotPermitted / assistive access
            "-1743",  # errAEEventWouldRequireUserConsent / not authorized
            "-25211",
            "1002",  # automation (Apple Events) denial
        )
    )


def _subprocess_runner(args: list[str]) -> RunResult:
    """Default runner: run ``args`` (no shell) and return (code, output)."""
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def _app_installed(name: str) -> bool:
    """Whether an app bundle named ``name`` exists in a standard location.

    A plain filesystem check (no permissions, no AppleScript) so we never reference
    an uninstalled app — doing so makes AppleScript pop a "Choose Application" picker.
    """
    from pathlib import Path

    roots = (
        Path("/Applications"),
        Path.home() / "Applications",
        Path("/System/Applications"),
        Path("/System/Cryptexes/App/System/Applications"),  # Safari on modern macOS
    )
    return any((root / f"{name}.app").exists() for root in roots)


class AppTools:
    """macOS application lifecycle operations exposed as tools."""

    def __init__(
        self,
        runner: Runner | None = None,
        is_installed: Callable[[str], bool] | None = None,
    ) -> None:
        self._run = runner or _subprocess_runner
        self._is_installed = is_installed or _app_installed

    def _osa(self, script: str, *args: str) -> RunResult:
        """Run an AppleScript, passing ``args`` as the script's run arguments."""
        return self._run(["osascript", "-e", script, *args])

    @staticmethod
    def _ok(rc: int, out: str, success: str, failure: str) -> str:
        """Pick a friendly message from a run result.

        A permission denial returns an actionable hint (the user must grant
        access), since that's by far the most common — and only user-fixable —
        failure for these tools.
        """
        if rc == 0:
            return success
        if _is_permission_error(out):
            return _PERMISSION_HINT
        detail = f" ({out})" if out else ""
        return f"{failure}{detail}"

    def _launch(self, name: str, success: str) -> str:
        """Open/activate an app via `open -a` (fuzzy name match, never hangs).

        `open -a` resolves close names (e.g. 'VS Code' -> Visual Studio Code),
        launches the app if needed, brings it to the front, and reopens a
        minimized window — all without the hang that AppleScript `tell
        application <name>` causes on a name it can't resolve exactly.
        """
        rc, out = self._run(["open", "-a", name])
        if rc != 0 and _is_not_installed(out):
            return f"{name} doesn't appear to be installed."
        return self._ok(rc, out, success, f"Couldn't open {name}")

    def open_app(self, name: str) -> str:
        """Launch an app (or bring it forward if already running)."""
        return self._launch(name, f"Opened {name}.")

    def open_website(self, url: str) -> str:
        """Open a URL in the default browser via ``open <url>`` (https by default)."""
        target = url.strip()
        if not target:
            return "No website given."
        if not re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            target = "https://" + target  # bare domain like "youtube.com"
        rc, out = self._run(["open", target])
        return self._ok(rc, out, f"Opened {target}", f"Couldn't open {target}")

    def close_website(self, url: str) -> str:
        """Close browser tab(s) showing ``url`` — the matching tabs only, not the app."""
        query = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I).split("/")[0].strip()
        if not query:
            return "No website given."
        # Only script browsers that are installed (filesystem check — referencing an
        # uninstalled app pops a "Choose Application" picker). The script's own
        # `is running` guard handles installed-but-closed browsers (nothing to close).
        total = 0
        scripted = 0
        for browser in _TAB_BROWSERS:
            if not self._is_installed(browser):
                _log.debug("close_website skip browser=%s (not installed)", browser)
                continue
            scripted += 1
            rc, out = self._osa(_close_tab_script(browser), query)
            closed = int("".join(ch for ch in out if ch.isdigit()) or 0) if rc == 0 else 0
            # Log the raw result per browser so failures (permission, no-match,
            # script error) are diagnosable from the debug report.
            _log.info(
                "close_website browser=%s query=%r rc=%d closed=%d detail=%r",
                browser,
                query,
                rc,
                closed,
                out[:200],
            )
            if rc != 0:
                if _is_permission_error(out):
                    return _PERMISSION_HINT
                continue
            total += closed
        if total:
            return f"Closed {total} tab{'s' if total != 1 else ''} for {query}."
        if scripted == 0:
            return f"I couldn't find a browser to close {query} in."
        return f"I couldn't find an open {query} tab."

    def focus_app(self, name: str) -> str:
        """Switch to / show an app, restoring it if minimized."""
        return self._launch(name, f"Switched to {name}.")

    def hide_app(self, name: str) -> str:
        """Hide an app's windows."""
        rc, out = self._osa(_HIDE, name)
        return self._ok(rc, out, f"Hid {name}.", f"Couldn't hide {name}")

    @staticmethod
    def _guard(name: str, out: str) -> str | None:
        """Map a script's no-op return ('no window'/'not running') to a message."""
        if out == "not running":
            return f"{name} isn't running."
        if out == "no window":
            return f"{name} has no open window."
        return None

    def minimize_app(self, name: str) -> str:
        """Minimize an app's front window to the Dock."""
        rc, out = self._osa(_MINIMIZE, name)
        if rc == 0 and (guarded := self._guard(name, out)):
            return guarded
        return self._ok(rc, out, f"Minimized {name}.", f"Couldn't minimize {name}")

    def maximize_app(self, name: str) -> str:
        """Make an app's front window full-screen."""
        rc, out = self._osa(_MAXIMIZE, name)
        if rc == 0 and (guarded := self._guard(name, out)):
            return guarded
        return self._ok(rc, out, f"Maximized {name}.", f"Couldn't maximize {name}")

    def quit_app(self, name: str) -> str:
        """Quit an app gracefully."""
        rc, out = self._osa(_QUIT, name)
        return self._ok(rc, out, f"Quit {name}.", f"Couldn't quit {name}")

    def list_apps(self) -> str:
        """List the apps that currently have visible windows."""
        rc, out = self._osa(_LIST)
        if rc != 0:
            if _is_permission_error(out):
                return _PERMISSION_HINT
            detail = f" ({out})" if out else ""
            return f"Couldn't list apps{detail}"
        names = [n.strip() for n in out.split(",") if n.strip()]
        return "Open apps: " + ", ".join(names) if names else "No foreground apps are open."

    def uninstall_app(self, name: str) -> str:
        """Move an app from /Applications to the Trash (recoverable from Trash)."""
        rc, out = self._osa(_UNINSTALL, name)
        return self._ok(rc, out, f"Moved {name} to the Trash.", f"Couldn't uninstall {name}")

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs with risk levels for the permission gate."""
        name_param = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The application's name, e.g. 'Safari'."}
            },
            "required": ["name"],
        }
        return [
            ToolSpec(
                name="open_app",
                description="Open / launch / start an app by name (e.g. 'open Spotify').",
                parameters=name_param,
                handler=self.open_app,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="open_website",
                description=(
                    "Open a website or online service in the browser (e.g. 'open "
                    "YouTube' -> youtube.com, 'open Gmail', 'go to apple.com', 'pull up "
                    "the news'). Use this — not open_app — whenever the user names a "
                    "website or a service that lives on the web rather than an installed "
                    "app. A bare domain is fine; it's opened as https."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL or domain, e.g. 'youtube.com'.",
                        }
                    },
                    "required": ["url"],
                },
                handler=self.open_website,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="close_website",
                description=(
                    "Close the browser tab(s) showing a website (e.g. 'close "
                    "chatgpt.com', 'close that YouTube tab', 'close the page you just "
                    "opened'). Closes only the matching tabs across Safari/Chrome/Edge "
                    "— NOT the whole browser. Use this to close a site opened with "
                    "open_website; only use quit_app to close an entire application."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Site/domain to close, e.g. 'chatgpt.com'.",
                        }
                    },
                    "required": ["url"],
                },
                handler=self.close_website,
                risk=Risk.WRITE,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="focus_app",
                description=(
                    "Switch to / show an app: bring it to the front, restoring its "
                    "window if it was minimized (e.g. 'switch to Safari', 'show "
                    "Spotify', 'open it again', 'I can't see it'). Not for closing."
                ),
                parameters=name_param,
                handler=self.focus_app,
                risk=Risk.WRITE,
            ),
            ToolSpec(
                name="hide_app",
                description="Hide a running app's windows.",
                parameters=name_param,
                handler=self.hide_app,
                risk=Risk.WRITE,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="minimize_app",
                description="Minimize an app's front window to the Dock.",
                parameters=name_param,
                handler=self.minimize_app,
                risk=Risk.WRITE,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="maximize_app",
                description="Make an app's front window full-screen.",
                parameters=name_param,
                handler=self.maximize_app,
                risk=Risk.WRITE,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="quit_app",
                description=(
                    "Close / quit / exit / shut down a whole running app (e.g. 'close "
                    "Spotify', 'quit Mail'). Never tell the user to click the X. But to "
                    "close a website or a browser tab, use close_website — quit_app "
                    "would close the entire browser and all its other tabs."
                ),
                parameters=name_param,
                handler=self.quit_app,
                risk=Risk.WRITE,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="list_apps",
                description="List the apps that currently have visible windows.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_apps,
                risk=Risk.READ_ONLY,
                requires=AUTOMATION,
            ),
            ToolSpec(
                name="uninstall_app",
                description=(
                    "Uninstall an app by moving it from /Applications to the Trash. "
                    "Destructive — always confirm with the user first."
                ),
                parameters=name_param,
                handler=self.uninstall_app,
                risk=Risk.DESTRUCTIVE,
                requires=AUTOMATION,
            ),
        ]


def register_app_tools(registry: ToolRegistry, runner: Runner | None = None) -> AppTools:
    """Register the macOS app-control tools into ``registry``.

    Returns:
        The :class:`AppTools` instance, for reference.
    """
    tools = AppTools(runner)
    for spec in tools.specs():
        registry.register(spec)
    _log.info(
        "app-control tools registered (open/website/close-tab/focus/hide/quit/list/uninstall)"
    )
    return tools
