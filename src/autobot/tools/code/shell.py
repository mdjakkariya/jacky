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

from autobot.core.streaming import output_sink
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.command_policy import classify_command
from autobot.tools.code.output_budget import budget_output
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("coder")

_DEFAULT_TIMEOUT = 120.0  # seconds
_MAX_TIMEOUT = 600.0

# (command, cwd, timeout, on_output) -> (returncode, combined_output, timed_out). The
# ``on_output`` sink (if any) is called with each output line as it arrives, so the CLI can
# stream progress live. Injectable for tests.
CommandRunner = Callable[[str, str, float, "Callable[[str], None] | None"], tuple[int, str, bool]]


def _streaming_runner(  # pragma: no cover - the real OS boundary; tests inject a fake runner
    command: str, cwd: str, timeout: float, on_output: Callable[[str], None] | None
) -> tuple[int, str, bool]:
    """Run ``command`` in the platform shell, streaming each output line to ``on_output``.

    Reads combined stdout/stderr on a reader thread so a deadline can enforce the timeout
    and kill the whole process group (dev servers spawn children). Never raises.
    """
    import subprocess
    import threading

    if sys.platform == "win32":
        proc = subprocess.Popen(
            ["cmd", "/c", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    lines: list[str] = []

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if on_output is not None:
                on_output(line.rstrip("\n"))

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait()
    reader.join(timeout=1.0)
    return (124 if timed_out else proc.returncode), "".join(lines), timed_out


def _kill_tree(proc: object) -> None:  # pragma: no cover - the real OS boundary
    """Kill a process and its children (POSIX process group / Windows tree). Never raises."""
    import os
    import signal
    import subprocess

    pid = proc.pid  # type: ignore[attr-defined]
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"], check=False)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def run_command(
    command: str,
    broker: AccessBroker,
    cwd: str = ".",
    timeout: float = _DEFAULT_TIMEOUT,
    runner: CommandRunner | None = None,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    output_model_cap: int = 10_000,
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
    run = runner or _streaming_runner
    sink = output_sink.get()  # set by the harness for the duration of this tool call
    try:
        rc, out, timed_out = run(command, str(workdir), limit, sink)
    except OSError as exc:  # spawn failure (missing shell, etc.)
        return f"I couldn't run that command: {exc}"
    _log.info("run_command rc=%d timed_out=%s chars=%d", rc, timed_out, len(out))
    # The human already saw the full output live (via the sink); hand the model a bounded,
    # tail-biased result, spilling anything large to a file it can read_file/grep.
    body = budget_output(out, cwd=workdir, cap=output_model_cap)
    if timed_out:
        return f"Command timed out after {int(limit)}s (partial output):\n{body}"
    status = "ok" if rc == 0 else f"exit {rc}"
    return f"[{status}]\n{body}" if body.strip() else f"[{status}] (no output)"


def register_exec_tools(
    registry: ToolRegistry,
    broker: AccessBroker,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    output_model_cap: int = 10_000,
) -> None:
    """Register the execution tool (run_command). Destructive → the gate confirms it.

    Args:
        registry: Tool registry to register into.
        broker: Access broker enforcing the workspace jail.
        allowlist: Commands pre-approved by the user to run without confirmation.
        blocklist: Commands the user has pre-refused; always blocked.
        output_model_cap: Max chars of output returned to the model inline (larger output
            is spilled to a file and only an excerpt + path is returned).
    """
    registry.register(
        ToolSpec(
            name="run_command",
            description=(
                "Run a shell command (e.g. tests, a build, git, a linter) in the working "
                "folder and return its output. Cross-platform. Prefer the dedicated tools "
                "(read_file/edit_file/grep/glob) over shelling out for file work. Output over "
                "~10k chars is capped automatically (the full output is saved to a file and an "
                "excerpt returned), so you do NOT need to shorten it yourself. Do NOT pipe a "
                "long or verbose command through `| tail`/`| head`: that buffers ALL its output "
                "until the command finishes, so nothing streams and it looks stuck. Run tests/"
                "builds directly — their output streams live line-by-line and is budgeted for "
                "you (use a streaming reporter for a big suite, e.g. `npx playwright test "
                "--reporter=line`). Use `grep` only to *filter* output you truly don't need. "
                "For a long-running process (e.g. a dev server) needed by a later step, start "
                "it in the background with output redirected to a file (e.g. `nohup <cmd> > "
                "/tmp/server.log 2>&1 &`), then continue."
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
                command,
                broker,
                cwd,
                timeout,
                allowlist=allowlist,
                blocklist=blocklist,
                output_model_cap=output_model_cap,
            ),
            risk=Risk.DESTRUCTIVE,
            ack="Running that command.",
        )
    )
    _log.info("exec tools registered (run_command)")
