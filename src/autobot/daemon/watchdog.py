"""Exit the engine when the launching app (the orb) goes away.

The orb spawns the engine as a sidecar. If the orb quits, force-quits, or crashes,
the engine must not linger holding the WebSocket port. We can't rely on the orb
killing us: the frozen engine is a PyInstaller *onefile* binary, which runs as two
processes — a bootloader parent and the real Python child — and Tauri's
``child.kill()`` only reaches the bootloader, orphaning the Python process that
actually holds the port. So instead the orb passes its own PID via
``AUTOBOT_PARENT_PID`` and this watchdog polls it; once that process is gone, the
engine shuts itself down. This covers every quit path (clean quit, force-quit,
crash), not just a forwarded signal.

The polling logic is pure and dependency-injected so it can be unit-tested without
spawning real processes.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping

from autobot.logging_setup import get_logger

_log = get_logger("daemon")

_ENV_PARENT_PID = "AUTOBOT_PARENT_PID"


def _process_alive(pid: int) -> bool:
    """Return whether a process with ``pid`` currently exists.

    ``signal 0`` performs the kernel's existence/permission checks without sending
    a signal: no error means it's alive, ``ProcessLookupError`` means it's gone, and
    ``PermissionError`` means it exists but isn't ours to signal (still alive).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parent_pid_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    """Parse ``AUTOBOT_PARENT_PID``; ``None`` if unset or not a positive integer."""
    env = os.environ if environ is None else environ
    raw = env.get(_ENV_PARENT_PID, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def watch_parent(
    pid: int,
    on_exit: Callable[[], None],
    *,
    interval_s: float = 1.0,
    is_alive: Callable[[int], bool] = _process_alive,
    sleep: Callable[[float], None] = time.sleep,
    should_continue: Callable[[], bool] = lambda: True,
) -> None:
    """Block until ``pid`` disappears, then call ``on_exit`` exactly once.

    The injectable ``is_alive``/``sleep``/``should_continue`` make the loop testable
    without real processes or wall-clock waits.
    """
    while should_continue():
        if not is_alive(pid):
            _log.warning("parent process %d gone — shutting down engine", pid)
            on_exit()
            return
        sleep(interval_s)


def start_parent_watchdog(
    on_exit: Callable[[], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> threading.Thread | None:
    """Arm the watchdog if the orb gave us its PID; otherwise a no-op (returns None).

    Defaults to a hard ``os._exit(0)`` so the engine dies promptly even if uvicorn
    or a model thread would otherwise hang shutdown — the parent is already gone, so
    there's nothing to drain.
    """
    pid = parent_pid_from_env(environ)
    if pid is None:
        return None
    exit_fn = on_exit or (lambda: os._exit(0))
    _log.info("parent watchdog armed for pid=%d", pid)
    thread = threading.Thread(
        target=watch_parent,
        args=(pid, exit_fn),
        name="parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread
