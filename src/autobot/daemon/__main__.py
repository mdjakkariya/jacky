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
    main()
