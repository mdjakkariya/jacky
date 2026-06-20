"""Tests for the macOS app-control tools (no real processes spawned)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.apps import AppTools, register_app_tools
from autobot.tools.registry import ToolRegistry


class FakeRunner:
    """Records the argv it was called with and returns a canned result."""

    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.rc = rc
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.rc, self.out


def test_open_app_uses_open_dash_a() -> None:
    runner = FakeRunner()
    tools = AppTools(runner)
    assert tools.open_app("Safari") == "Opened Safari."
    assert runner.calls == [["open", "-a", "Safari"]]


def test_osascript_passes_name_as_argument_not_code() -> None:
    runner = FakeRunner()
    tools = AppTools(runner)
    tools.quit_app("Spotify")
    argv = runner.calls[0]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    # The app name is the trailing run-argument, never spliced into the script.
    assert argv[-1] == "Spotify"
    assert "Spotify" not in argv[2]  # not in the script body


def test_quit_focus_hide_min_max_report_success() -> None:
    tools = AppTools(FakeRunner(rc=0))
    assert tools.quit_app("Mail") == "Quit Mail."
    assert tools.hide_app("Mail") == "Hid Mail."
    assert tools.minimize_app("Mail") == "Minimized Mail."
    assert tools.maximize_app("Mail") == "Maximized Mail."


def test_failure_includes_detail() -> None:
    tools = AppTools(FakeRunner(rc=1, out="boom"))
    assert tools.open_app("Nope") == "Couldn't open Nope (boom)"


def test_open_reports_not_installed_clearly() -> None:
    out = "Unable to find application named 'YouTube'."
    tools = AppTools(FakeRunner(rc=1, out=out))
    assert tools.open_app("YouTube") == "YouTube doesn't appear to be installed."


def test_permission_error_returns_actionable_hint() -> None:
    # macOS Accessibility denial -> guide the user to grant access (not a raw error).
    denial = (
        "execution error: System Events got an error: osascript is not allowed "
        "assistive access. (-1719)"
    )
    tools = AppTools(FakeRunner(rc=1, out=denial))
    msg = tools.minimize_app("Spotify")
    assert "Accessibility" in msg and "System Settings" in msg
    assert "-1719" not in msg  # no raw error leaked to the user


def test_minimize_targets_the_apps_own_window_not_a_keystroke() -> None:
    # Must act on the target app's window via AXMinimized — NEVER a global
    # keystroke (which can hit whatever is frontmost, e.g. the terminal).
    runner = FakeRunner()
    AppTools(runner).minimize_app("Spotify")
    script = runner.calls[0][2]
    assert "AXMinimized" in script
    assert "keystroke" not in script
    assert runner.calls[0][-1] == "Spotify"


def test_minimize_reports_no_window_truthfully() -> None:
    # Script returns "no window" with exit 0 — must not be reported as success.
    tools = AppTools(FakeRunner(rc=0, out="no window"))
    assert tools.minimize_app("Spotify") == "Spotify has no open window."
    tools2 = AppTools(FakeRunner(rc=0, out="not running"))
    assert tools2.maximize_app("Spotify") == "Spotify isn't running."


def test_focus_uses_open_to_resolve_names_without_hanging() -> None:
    # Focus must use `open -a` (fuzzy, never hangs) — not AppleScript `tell
    # application <name>`, which hangs on an inexact name like "VS Code".
    runner = FakeRunner()
    assert AppTools(runner).focus_app("VS Code") == "Switched to VS Code."
    assert runner.calls == [["open", "-a", "VS Code"]]


def test_focus_reports_not_installed_when_name_unresolvable() -> None:
    out = "Unable to find application named 'VS Code'."
    tools = AppTools(FakeRunner(rc=1, out=out))
    assert tools.focus_app("VS Code") == "VS Code doesn't appear to be installed."


def test_maximize_restores_then_fullscreens_target_window() -> None:
    runner = FakeRunner()
    AppTools(runner).maximize_app("Spotify")
    script = runner.calls[0][2]
    assert "reopen" in script  # bring it on screen first
    assert "AXFullScreen" in script
    assert "keystroke" not in script  # no global keystroke that could hit the terminal


def test_list_apps_parses_comma_list() -> None:
    tools = AppTools(FakeRunner(out="Safari, Mail, Notes"))
    assert tools.list_apps() == "Open apps: Safari, Mail, Notes"


def test_list_apps_handles_empty() -> None:
    tools = AppTools(FakeRunner(out=""))
    assert tools.list_apps() == "No foreground apps are open."


def test_uninstall_reports_trash() -> None:
    tools = AppTools(FakeRunner(rc=0))
    assert tools.uninstall_app("Foo") == "Moved Foo to the Trash."


def test_risk_levels_match_policy() -> None:
    specs = {s.name: s for s in AppTools(FakeRunner()).specs()}
    assert specs["list_apps"].risk is Risk.READ_ONLY
    assert specs["open_app"].risk is Risk.WRITE
    assert specs["quit_app"].risk is Risk.WRITE
    # Only uninstall is destructive (so only it prompts for confirmation).
    assert specs["uninstall_app"].risk is Risk.DESTRUCTIVE


def test_register_adds_all_tools() -> None:
    registry = ToolRegistry()
    register_app_tools(registry, FakeRunner())
    for name in (
        "open_app",
        "focus_app",
        "hide_app",
        "minimize_app",
        "maximize_app",
        "quit_app",
        "list_apps",
        "uninstall_app",
    ):
        assert registry.get(name) is not None
