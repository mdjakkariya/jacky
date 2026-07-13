"""PyInstaller entry for the frozen ``jack`` binary — runs the CLI (client or ``serve``).

Kept out of the package as a thin launcher so the spec has a single script to analyze
and the ``multiprocessing`` guard is unambiguous. The guard is CRITICAL for frozen
builds: on macOS ``spawn`` re-executes this entry for every worker a dependency starts,
so without ``freeze_support`` a spawned child would re-run the CLI instead of its
worker target.
"""

from __future__ import annotations

import multiprocessing
import sys

from autobot.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
