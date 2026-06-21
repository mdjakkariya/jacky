"""Tests for the orb presence tool (voice dismiss)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.orb import register_orb_tools
from autobot.tools.registry import ToolRegistry


def test_dismiss_is_registered_with_write_risk() -> None:
    registry = ToolRegistry()
    register_orb_tools(registry, lambda: None)
    spec = registry.get("dismiss")
    assert spec is not None
    assert spec.risk is Risk.WRITE


def test_dismiss_invokes_hide_and_returns_a_reply() -> None:
    calls: list[bool] = []
    registry = ToolRegistry()
    register_orb_tools(registry, lambda: calls.append(True))
    spec = registry.get("dismiss")
    assert spec is not None

    reply = spec.handler()

    assert calls == [True]  # the hide sink was triggered
    assert isinstance(reply, str) and reply
