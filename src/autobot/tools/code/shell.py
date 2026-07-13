"""Cross-platform command execution for the coder profile (gated, cwd-jailed).

``run_command`` runs one shell command in a jailed working directory and returns its
combined output, bounded. The command is genuinely powerful (a shell can reach outside
the cwd), so the tool is classified destructive and the permission gate is what contains
it — the cwd jail only sets where it starts. A ``runner`` seam is injected so command
assembly is unit-tested without spawning a real process; the default runner picks the
platform shell (``/bin/sh -c`` on Unix, ``cmd /c`` on Windows), applies the timeout, and
returns whether it timed out.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.command_policy import classify_command
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("coder")

_DEFAULT_TIMEOUT = 120.0  # seconds
_MAX_TIMEOUT = 600.0
_OUTPUT_CAP = 30_000  # max chars of combined output returned

# (command, cwd, timeout) -> (returncode, combined_output, timed_out). Injectable for tests.
CommandRunner = Callable[[str, str, float], tuple[int, str, bool]]


def _default_runner(  # pragma: no cover - the real OS boundary; tests inject a fake runner
    command: str, cwd: str, timeout: float
) -> tuple[int, str, bool]:
    """Run ``command`` in the platform shell, capturing combined output (never raises)."""
    import subprocess

    argv = ["cmd", "/c", command] if sys.platform == "win32" else ["/bin/sh", "-c", command]
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, out + err, True
    combined = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, combined, False


def run_command(
    command: str,
    broker: AccessBroker,
    cwd: str = ".",
    timeout: float = _DEFAULT_TIMEOUT,
    runner: CommandRunner | None = None,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> str:
    """Run a shell ``command`` in a jailed ``cwd`` (gated), returning bounded output.

    Before running, ``command`` is classified against the built-in dangerous-command
    baseline and the user ``allowlist``/``blocklist`` (see
    :func:`autobot.tools.code.command_policy.classify_command`); a ``"block"``
    decision refuses to run it at all. This is a last-resort backstop — the
    permission gate (``Risk.DESTRUCTIVE``) still confirms everything that isn't
    blocked.
    """
    if not command or not command.strip():
        return "What command should I run?"
    decision, reason = classify_command(command, allowlist, blocklist)
    if decision == "block":
        return f"That command is blocked for safety ({reason})."
    try:
        workdir = broker.ensure(cwd or ".", write=True)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not workdir.is_dir():
        return f"'{workdir.name}' is not a folder to run in."
    limit = max(1.0, min(timeout or _DEFAULT_TIMEOUT, _MAX_TIMEOUT))
    run = runner or _default_runner
    try:
        rc, out, timed_out = run(command, str(workdir), limit)
    except OSError as exc:  # spawn failure (missing shell, etc.)
        return f"I couldn't run that command: {exc}"
    body = out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP] + "\n…(output truncated)"
    _log.info("run_command rc=%d timed_out=%s chars=%d", rc, timed_out, len(out))
    if timed_out:
        return f"Command timed out after {int(limit)}s (partial output):\n{body}"
    status = "ok" if rc == 0 else f"exit {rc}"
    return f"[{status}]\n{body}" if body.strip() else f"[{status}] (no output)"


def register_exec_tools(
    registry: ToolRegistry,
    broker: AccessBroker,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> None:
    """Register the execution tool (run_command). Destructive → the gate confirms it.

    Args:
        registry: Tool registry to register into.
        broker: Access broker enforcing the workspace jail.
        allowlist: Commands pre-approved by the user to run without confirmation.
        blocklist: Commands the user has pre-refused; always blocked.
    """
    registry.register(
        ToolSpec(
            name="run_command",
            description=(
                "Run a shell command (e.g. tests, a build, git, a linter) in the working "
                "folder and return its output. Cross-platform. Prefer the dedicated tools "
                "(read_file/edit_file/grep/glob) over shelling out for file work. Long-running "
                "or interactive commands aren't supported; keep it to commands that finish."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "cwd": {"type": "string", "description": "Folder to run in (optional)."},
                    "timeout": {
                        "type": "number",
                        "description": "Seconds before it's killed (default 120, max 600).",
                    },
                },
                "required": ["command"],
            },
            handler=lambda command="", cwd=".", timeout=_DEFAULT_TIMEOUT: run_command(
                command, broker, cwd, timeout, allowlist=allowlist, blocklist=blocklist
            ),
            risk=Risk.DESTRUCTIVE,
            ack="Running that command.",
        )
    )
    _log.info("exec tools registered (run_command)")
