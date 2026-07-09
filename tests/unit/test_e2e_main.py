"""E2E CLI arg parsing + judge-mode resolution."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e.__main__ import build_parser, resolve_judge_mode


def test_parser_defaults() -> None:
    ns = build_parser().parse_args([])
    assert ns.command in (None, "run") and ns.port and ns.judge is None


def test_parser_names_and_flags() -> None:
    ns = build_parser().parse_args(["create-file", "edit-file", "--judge", "auto", "--keep"])
    assert ns.names == ["create-file", "edit-file"] and ns.judge == "auto" and ns.keep is True


def test_resolve_judge_mode_explicit_and_interactive() -> None:
    assert resolve_judge_mode("auto", isatty=False, ask=lambda: "m") == "auto"
    assert resolve_judge_mode(None, isatty=True, ask=lambda: "manual") == "manual"
    # non-interactive with no key preference falls back to manual
    assert resolve_judge_mode(None, isatty=False, ask=lambda: "auto") == "manual"
