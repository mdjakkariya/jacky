"""`python -m autobot.e2e` — run scenarios, or judge/re-judge a saved bundle.

Dev-only. Default command runs the corpus (or named scenarios) through the real TUI and
prints a scoreboard + artifact paths. ``judge <dir>`` runs the auto-judge on a saved bundle.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from autobot.e2e import runner, scenarios
from autobot.e2e.judge import judge_auto
from autobot.logging_setup import get_logger

_log = get_logger("e2e")
_DEFAULT_PORT = 8790  # isolated from the real coder daemon (8766)


def build_parser() -> argparse.ArgumentParser:
    """The `autobot.e2e` argument parser.

    ``command`` is not a separate positional: an optional ``nargs="?"`` slot ahead of a
    ``nargs="*"`` one would greedily swallow the first scenario name (e.g. ``create-file``)
    as the command. Instead ``names`` alone captures everything positional, and ``main``
    sniffs a leading literal ``"run"``/``"judge"`` off it; ``command`` defaults to ``"run"``.
    """
    p = argparse.ArgumentParser(prog="autobot.e2e", description="Dev-only CLI E2E harness.")
    p.add_argument(
        "names",
        nargs="*",
        help="scenario names (default: all); or 'judge <artifact-dir>' to grade a saved bundle",
    )
    p.add_argument("--judge", choices=["auto", "manual"], default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--port", type=int, default=_DEFAULT_PORT)
    p.add_argument("--keep", action="store_true", help="preserve the throwaway workspace")
    p.set_defaults(command="run")
    return p


def resolve_judge_mode(arg: str | None, *, isatty: bool, ask: Callable[[], str]) -> str:
    """Explicit flag wins; else ask when interactive; else default to manual."""
    if arg in ("auto", "manual"):
        return arg
    if isatty:
        return "manual" if ask().strip().lower().startswith("m") else "auto"
    return "manual"


def _ask() -> str:  # pragma: no cover - interactive prompt
    return input("Judge automatically [a] or manually [m]? ")


def _run_judge(bundle_dir: str, judge_model: str | None) -> int:
    """Run (or re-run) the auto-judge on a saved bundle directory and print the verdict."""
    bundle = Path(bundle_dir).expanduser()
    manifest = json.loads((bundle / "manifest.json").read_text())
    verdict = judge_auto(
        manifest["name"],
        manifest["task"],
        manifest.get("criteria", ""),
        (bundle / "screen.txt").read_text(),
        manifest.get("checks", []),
        judge_model=judge_model,
    )
    (bundle / "judge.json").write_text(json.dumps(verdict, indent=2))
    print(f"verdict: {'PASS' if verdict.get('pass') else 'FAIL'} — {bundle / 'judge.json'}")
    return 0 if verdict.get("pass") else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch to the `run` or `judge` subcommand."""
    ns = build_parser().parse_args(argv)
    names: list[str] = list(ns.names)
    command: str = ns.command
    if names and names[0] in ("run", "judge"):
        command, names = names[0], names[1:]

    if command == "judge":
        if not names:
            print("usage: python -m autobot.e2e judge <artifact-dir>", file=sys.stderr)
            return 2
        return _run_judge(names[0], ns.judge_model)

    mode = resolve_judge_mode(ns.judge, isatty=sys.stdin.isatty(), ask=_ask)
    chosen = [scenarios.by_name(n) for n in names] if names else list(scenarios.all_scenarios())
    results = runner.run_suite(
        chosen, port=ns.port, judge_mode=mode, judge_model=ns.judge_model, keep=ns.keep
    )
    print("\n=== E2E scoreboard ===")
    for r in results:
        print(f"  {'PASS' if r.passed else 'FAIL'}  {r.name:20s}  {r.report_path}")
    if mode == "manual":
        print("\nManual judging: open each report.md and verify with your own LLM/account.")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
