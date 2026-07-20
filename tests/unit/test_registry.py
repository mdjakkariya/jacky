"""Tests for ToolRegistry dispatch, incl. ToolError -> failed result mapping."""

from __future__ import annotations

from collections.abc import Callable

from autobot.core.types import Risk
from autobot.tools.registry import ToolError, ToolFailure, ToolRegistry, ToolSpec


def _registry_with(handler: Callable[..., str], name: str = "t") -> ToolRegistry:
    reg = ToolRegistry()
    spec = ToolSpec(name=name, description="", parameters={}, handler=handler, risk=Risk.WRITE)
    reg.register(spec)
    return reg


def test_tool_error_becomes_failed_result_without_prefix() -> None:
    def boom(**_kw: object) -> str:
        raise ToolError("no file named x; nothing was removed")

    result = _registry_with(boom).dispatch("t", {})
    assert result.ok is False
    assert result.content == "no file named x; nothing was removed"
    assert "tool failed" not in result.content


def test_unexpected_exception_still_prefixed() -> None:
    def boom(**_kw: object) -> str:
        raise ValueError("kaboom")

    result = _registry_with(boom).dispatch("t", {})
    assert result.ok is False
    assert result.content.startswith("tool failed:")


def test_successful_handler_is_ok() -> None:
    result = _registry_with(lambda **_kw: "done").dispatch("t", {})
    assert result.ok is True and result.content == "done"


def test_tool_failure_return_becomes_failed_result() -> None:
    # A handler that *returns* a ToolFailure (instead of raising) is still ok=False.
    result = _registry_with(lambda **_kw: ToolFailure("could not find it")).dispatch("t", {})
    assert result.ok is False
    assert result.content == "could not find it"
    assert "tool failed" not in result.content


def test_tool_failure_marker_does_not_leak_into_content() -> None:
    # The failure marker is normalised to a plain str in the result content.
    result = _registry_with(lambda **_kw: ToolFailure("nope")).dispatch("t", {})
    assert type(result.content) is str
