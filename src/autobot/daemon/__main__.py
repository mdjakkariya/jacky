"""Enable ``python -m autobot.daemon`` to launch the headless daemon.

Usage:
    python -m autobot.daemon                              # real engine behind the daemon
    python -m autobot.daemon --demo                        # scripted state cycle, no mic/model
    python -m autobot.daemon --profile coder --port 8766   # a coder daemon on its own port
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Any

from autobot.config import Settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse daemon CLI flags.

    ``--profile`` and ``--port`` default to ``None`` so :func:`_settings_from_args`
    can tell "not provided" apart from an explicit value and leave the rest of
    :class:`~autobot.config.Settings` untouched.
    """
    parser = argparse.ArgumentParser(prog="python -m autobot.daemon")
    parser.add_argument(
        "--profile",
        default=None,
        help="agent profile to run, e.g. 'coder' (default: from settings.json/'assistant')",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="daemon WebSocket port (default: from settings.json/8765)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="coder workspace directory to jail to (default: the launch cwd)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="cycle orb states without the engine (no mic/model)",
    )
    return parser.parse_args(argv)


def _settings_from_args(base: Settings, args: argparse.Namespace) -> Settings:
    """Overlay ``--profile``/``--port`` onto ``base``, leaving unset flags alone."""
    overrides: dict[str, Any] = {}
    if args.profile is not None:
        overrides["profile"] = args.profile
    if args.port is not None:
        overrides["daemon_port"] = args.port
    if not overrides:
        return base
    return replace(base, **overrides)


def main(argv: list[str] | None = None) -> None:
    """Console entry point: ``--demo`` cycles states without the engine."""
    args = _parse_args(argv)
    try:
        # Imported here so a missing 'daemon' extra fails with a clear hint
        # rather than an opaque ImportError at startup.
        from autobot.daemon.runner import serve, serve_demo
    except ImportError as exc:
        raise SystemExit(
            "The daemon needs the 'daemon' extra: run `uv sync --extra daemon`."
        ) from exc
    # Set the workspace config overlay before loading, so this daemon's settings (and every
    # later reload) layer `<workspace>/.jack/settings.json` over the global file.
    if args.workspace:
        from pathlib import Path

        from autobot.config import set_workspace_overlay

        set_workspace_overlay(Path(args.workspace) / ".jack" / "settings.json")
    settings = _settings_from_args(Settings.load(), args)
    if args.demo:
        serve_demo(settings)
    else:
        serve(settings, workspace=args.workspace)


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
