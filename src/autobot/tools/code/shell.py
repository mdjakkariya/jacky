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

import contextlib
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from autobot.core.streaming import active_session_id, output_sink
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.code.command_policy import classify_command
from autobot.tools.code.output_budget import budget_output
from autobot.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from autobot.tasks import NotificationInbox, TaskRegistry

_log = get_logger("coder")

_DEFAULT_TIMEOUT = 120.0  # seconds
_MAX_TIMEOUT = 600.0
_BG_EXCERPT_CHARS = 800  # tail of a finished background command folded into the next turn

# (command, cwd, timeout, on_output, on_spawn=None) -> (returncode, combined_output, timed_out).
# ``on_output`` streams each output line live; the optional ``on_spawn`` is called with the
# process handle right after spawn, so a background command can be killed. Injectable for tests.
CommandRunner = Callable[..., tuple[int, str, bool]]


def _streaming_runner(  # pragma: no cover - the real OS boundary; tests inject a fake runner
    command: str,
    cwd: str,
    timeout: float,
    on_output: Callable[[str], None] | None,
    on_spawn: Callable[[object], None] | None = None,
) -> tuple[int, str, bool]:
    """Run ``command`` in the platform shell, streaming each output line to ``on_output``.

    Reads combined stdout/stderr on a reader thread so a deadline can enforce the timeout
    and kill the whole process group (dev servers spawn children). ``on_spawn`` (if given) is
    handed the live process right after it starts, so a caller can register a kill handle.
    Never raises.
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
    if on_spawn is not None:
        on_spawn(proc)
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


def _rel(path: Path, base: Path) -> str:
    """``path`` relative to ``base`` for display, or the absolute path if it's outside."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` chars of ``text`` (stripped), elided with a leading ``…``."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _start_background(
    command: str,
    workdir: Path,
    timeout: float,
    runner: CommandRunner,
    registry: TaskRegistry,
    inbox: NotificationInbox,
    session_id: str,
) -> str:
    """Spawn ``command`` off the turn: stream to a ``.jack/tasks`` log, notify on finish.

    Registers a ``kind="command"`` task, runs it on a daemon thread (which blocks on the
    process, not the turn), and on completion marks the registry and pushes a completion
    note to ``session_id``'s inbox — so the model learns the result at its next turn
    without polling. Returns immediately with the task id and log path.
    """
    task = registry.add(kind="command", session_id=session_id, label=f"$ {command[:80]}")
    log_dir = workdir / ".jack" / "tasks"
    with contextlib.suppress(OSError):  # a log we can't write must not stop the command
        log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rel_log = _rel(log_path, workdir)
    short = command[:80]

    def _worker() -> None:
        try:
            handle = log_path.open("w", encoding="utf-8")
        except OSError:
            handle = None

        def _to_file(line: str) -> None:
            if handle is not None:
                handle.write(line + "\n")
                handle.flush()

        def _register_killer(proc: object) -> None:
            registry.set_killer(task.id, lambda: _kill_tree(proc))

        prefix = f"Background command {task.id} (`{short}`)"
        try:
            rc, out, timed_out = runner(command, str(workdir), timeout, _to_file, _register_killer)
        except OSError as exc:  # spawn failure (missing shell, etc.)
            registry.mark_failed(task.id, result=f"couldn't run: {exc}", returncode=None)
            inbox.push(session_id, f"{prefix} failed to start: {exc}")
            return
        finally:
            if handle is not None:
                handle.close()
        excerpt = _tail(out, _BG_EXCERPT_CHARS)
        if timed_out:
            registry.mark_failed(task.id, result=f"timed out after {int(timeout)}s", returncode=124)
            note = f"{prefix} timed out after {int(timeout)}s. Full log: {rel_log}"
        elif rc == 0:
            registry.mark_done(task.id, result=excerpt, returncode=0)
            note = f"{prefix} finished OK (exit 0). Last output:\n{excerpt}\nFull log: {rel_log}"
        else:
            registry.mark_failed(task.id, result=excerpt, returncode=rc)
            note = f"{prefix} failed (exit {rc}). Last output:\n{excerpt}\nFull log: {rel_log}"
        inbox.push(session_id, note)
        _log.info("background task=%s rc=%d timed_out=%s", task.id, rc, timed_out)

    threading.Thread(target=_worker, name=f"bg-{task.id}", daemon=True).start()
    _log.info("run_command backgrounded task=%s cmd=%s", task.id, short)
    return (
        f"Started in the background as {task.id} (streaming to {rel_log}). It's running now; "
        "you'll get its result automatically at the start of your next step once it finishes. "
        "Do NOT wait or poll for it — continue with other work, or end your turn if nothing "
        "else can proceed until it's done."
    )


def run_command(
    command: str,
    broker: AccessBroker,
    cwd: str = ".",
    timeout: float = _DEFAULT_TIMEOUT,
    runner: CommandRunner | None = None,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    output_model_cap: int = 10_000,
    *,
    run_in_background: bool = False,
    registry: TaskRegistry | None = None,
    inbox: NotificationInbox | None = None,
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
    if run_in_background:
        if registry is not None and inbox is not None:
            sid = active_session_id.get()
            return _start_background(command, workdir, limit, run, registry, inbox, sid)
        _log.warning("run_in_background requested but no task registry wired; running foreground")
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


_TAIL_DEFAULT_LINES = 50  # lines returned by a background_tasks tail
_TAIL_MAX_CHARS = 8000  # char cap on a tail so a chatty log can't flood the turn


def _status_text(task: object) -> str:
    """Human status for a task row (``running`` or e.g. ``failed (exit 1)``)."""
    status = getattr(task, "status", "?")
    if status == "running":
        return "running"
    return f"{status} (exit {getattr(task, 'returncode', None)})"


def background_tasks(
    broker: AccessBroker,
    registry: TaskRegistry | None,
    action: str = "list",
    task_id: str = "",
    lines: int = _TAIL_DEFAULT_LINES,
) -> str:
    """List backgrounded commands and tail their output (read-only visibility into bg work).

    ``action="list"`` shows this session's background tasks (id, status, label); ``"tail"``
    returns the last ``lines`` of ``task_id``'s log. Lets the model check on a long-running
    process instead of re-running it. (Killing a task is not yet supported — see #100.)
    """
    if registry is None:
        return "Background tasks aren't available here."
    if action == "list":
        sid = active_session_id.get() or None
        tasks = [t for t in registry.list(session_id=sid) if t.kind == "command"]
        if not tasks:
            return "No background tasks."
        rows = [f"{t.id}  [{_status_text(t)}]  {t.label}" for t in tasks[:50]]
        return "Background tasks (newest first):\n" + "\n".join(rows)
    if action == "tail":
        if not task_id:
            return "Which task? Pass `task_id` (see the list action)."
        task = registry.get(task_id)
        if task is None:
            return f"No background task {task_id!r} (it may have been evicted)."
        try:
            base = broker.ensure(".", write=False)
        except (AccessDeniedError, PermissionError) as exc:
            return str(exc)
        log_path = base / ".jack" / "tasks" / f"{task_id}.log"
        if not log_path.exists():
            return f"No log yet for {task_id} (status: {_status_text(task)})."
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"I couldn't read {task_id}'s log: {exc}"
        tail_lines = content.splitlines()[-max(1, lines) :]
        text = "\n".join(tail_lines)
        if len(text) > _TAIL_MAX_CHARS:
            text = "…" + text[-_TAIL_MAX_CHARS:]
        return f"{task_id} [{_status_text(task)}] — last {len(tail_lines)} line(s):\n{text}"
    if action == "kill":
        if not task_id:
            return "Which task? Pass `task_id` (see the list action)."
        task = registry.get(task_id)
        if task is None:
            return f"No background task {task_id!r} (it may have been evicted)."
        if task.settled:
            return f"{task_id} already finished ({_status_text(task)}); nothing to kill."
        if registry.kill(task_id):
            return f"Killing {task_id}. Its result arrives at your next step once it stops."
        return f"Couldn't kill {task_id} — it may have just finished."
    return "action must be 'list', 'tail', or 'kill'."


def register_exec_tools(
    registry: ToolRegistry,
    broker: AccessBroker,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    output_model_cap: int = 10_000,
    task_registry: TaskRegistry | None = None,
    task_inbox: NotificationInbox | None = None,
) -> None:
    """Register the execution tool (run_command). Destructive → the gate confirms it.

    Args:
        registry: Tool registry to register into.
        broker: Access broker enforcing the workspace jail.
        allowlist: Commands pre-approved by the user to run without confirmation.
        blocklist: Commands the user has pre-refused; always blocked.
        output_model_cap: Max chars of output returned to the model inline (larger output
            is spilled to a file and only an excerpt + path is returned).
        task_registry: Process-global async-task registry. When set (with ``task_inbox``),
            ``run_command`` supports ``run_in_background`` — the command runs off the turn
            and its result is delivered on the next turn. ``None`` disables backgrounding.
        task_inbox: Per-session notification inbox that carries a backgrounded command's
            completion note back to the model's next turn.
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
                "A full test suite or build can take minutes: set a generous `timeout` (up to "
                "600 seconds) so it isn't killed mid-run — don't guard it with a short timeout "
                "and don't sleep-poll it. Set `run_in_background: true` for a command you should "
                "NOT wait on inline — a long process a later step depends on (e.g. a dev server), "
                "or slow work you can carry on around: it returns immediately and its result is "
                "delivered to you automatically at your next step, so never sleep-poll or re-run "
                "to check on it. (Prefer this over a manual `nohup <cmd> &`.)"
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
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run off the turn and return immediately; the result is delivered "
                            "at your next step. Use for a long process or a server (default false)."
                        ),
                    },
                },
                "required": ["command"],
            },
            handler=lambda command="", cwd=".", timeout=_DEFAULT_TIMEOUT, run_in_background=False: (
                run_command(
                    command,
                    broker,
                    cwd,
                    timeout,
                    allowlist=allowlist,
                    blocklist=blocklist,
                    output_model_cap=output_model_cap,
                    run_in_background=bool(run_in_background),
                    registry=task_registry,
                    inbox=task_inbox,
                )
            ),
            risk=Risk.DESTRUCTIVE,
            ack="Running that command.",
        )
    )
    if task_registry is not None:
        registry.register(
            ToolSpec(
                name="background_tasks",
                description=(
                    "Check on or stop commands you started with run_command "
                    "`run_in_background: true`. `action`: 'list' (default — id, status, label), "
                    "'tail' (pass `task_id`, optional `lines`, to see recent output), or 'kill' "
                    "(pass `task_id` to stop a running one). Use this instead of re-running a "
                    "long process."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "tail", "kill"],
                            "description": "'list' (default), 'tail', or 'kill'.",
                        },
                        "task_id": {"type": "string", "description": "Task id for 'tail'."},
                        "lines": {"type": "integer", "description": "Lines to tail (default 50)."},
                    },
                    "required": [],
                },
                handler=lambda action="list", task_id="", lines=_TAIL_DEFAULT_LINES: (
                    background_tasks(broker, task_registry, action, task_id, lines)
                ),
                risk=Risk.READ_ONLY,
                ack="Checking background tasks.",
            )
        )
    _log.info("exec tools registered (run_command background=%s)", task_registry is not None)
