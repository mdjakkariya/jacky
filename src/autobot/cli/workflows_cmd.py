"""``jack workflows`` — manage ``WORKFLOW.md`` workflows from a plain shell.

Workflows are pure filesystem objects (standard directories full of ``WORKFLOW.md`` files),
unlike MCP servers or the coder session — so, unlike ``jack mcp``, this command never spawns
or talks to a daemon. It builds a :class:`~autobot.workflows.registry.WorkflowRegistry`
directly and acts in-process.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from autobot.logging_setup import get_logger
from autobot.workflows.registry import WorkflowRegistry, default_workflow_dirs

_log = get_logger("cli")

_USAGE = """usage: jack workflows <verb>
  list              installed workflows (name + description)
  show <name>       print an installed workflow's full body
  run <name>        execute a workflow from a Jack coding turn"""


def _registry() -> WorkflowRegistry:
    """A fresh registry over the standard workflow directories (user + project)."""
    return WorkflowRegistry(default_workflow_dirs(Path.home(), Path.cwd()))


def _cmd_list(out: TextIO) -> int:
    specs = _registry().specs()
    if not specs:
        print("No workflows found.", file=out)
        return 0
    for spec in specs:
        print(f"{spec.name}  —  {spec.description}", file=out)
    return 0


def _cmd_show(name: str, out: TextIO, err: TextIO) -> int:
    spec = _registry().get(name)
    if spec is None:
        print(f"No workflow named {name}.", file=err)
        return 1
    print(spec.path.read_text(encoding="utf-8"), file=out)
    return 0


def _cmd_run(name: str, out: TextIO, err: TextIO) -> int:
    spec = _registry().get(name)
    if spec is None:
        print(f"No workflow named {name}.", file=err)
        return 1
    msg = (
        "Run a workflow from a Jack coding turn: ask Jack to run the '"
        f"{name}' workflow (it calls run_workflow). Direct CLI execution needs a "
        "turn context and isn't supported yet."
    )
    print(msg, file=out)
    return 0


def run(argv: list[str]) -> int:
    """Dispatch one ``jack workflows`` invocation.

    Args:
            argv: Everything after ``jack workflows`` (e.g. ``["list"]``).

    Returns:
            0 on success, 1 on a failed/not-found operation, 2 on a usage error.
    """
    out, err = sys.stdout, sys.stderr
    if not argv or argv[0] == "list":
        return _cmd_list(out)
    verb, rest = argv[0], argv[1:]
    _log.debug("jack workflows verb=%s", verb)
    if verb in ("--help", "-h", "help"):
        print(_USAGE, file=out)
        return 0
    if verb == "show" and len(rest) == 1:
        return _cmd_show(rest[0], out, err)
    if verb == "run" and len(rest) == 1:
        return _cmd_run(rest[0], out, err)
    print(_USAGE, file=err)
    return 2
