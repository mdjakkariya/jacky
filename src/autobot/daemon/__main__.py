"""Enable ``python -m autobot.daemon`` to launch the headless daemon.

Usage:
    python -m autobot.daemon          # real engine behind the daemon
    python -m autobot.daemon --demo   # scripted state cycle, no mic/model
"""

from __future__ import annotations

import sys


def main() -> None:
    """Console entry point: ``--demo`` cycles states without the engine."""
    try:
        # Imported here so a missing 'daemon' extra fails with a clear hint
        # rather than an opaque ImportError at startup.
        from autobot.daemon.runner import serve, serve_demo
    except ImportError as exc:
        raise SystemExit(
            "The daemon needs the 'daemon' extra: run `uv sync --extra daemon`."
        ) from exc
    if "--demo" in sys.argv[1:]:
        serve_demo()
    else:
        serve()


if __name__ == "__main__":
    # CRITICAL for frozen (PyInstaller) builds: on macOS multiprocessing defaults to
    # the 'spawn' start method, which re-executes THIS entry point for every worker a
    # dependency starts (e.g. parallel model downloads on first run). Without this
    # guard each spawned child would boot a brand-new engine — opening the mic and
    # loading STT — cascading into a fork bomb that pins the CPU and hangs the machine.
    # freeze_support() makes a spawned child run its worker target instead of main().
    import multiprocessing

    multiprocessing.freeze_support()
    main()
