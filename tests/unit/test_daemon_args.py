"""The daemon CLI arg parser exposes --workspace for the coder."""

from __future__ import annotations

from autobot.daemon.__main__ import _parse_args


def test_parse_workspace_arg() -> None:
    ns = _parse_args(["--profile", "coder", "--port", "8766", "--workspace", "/ws/x"])
    assert ns.workspace == "/ws/x"


def test_workspace_defaults_none() -> None:
    ns = _parse_args(["--profile", "coder"])
    assert ns.workspace is None
