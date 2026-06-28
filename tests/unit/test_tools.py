"""Tests for the tool registry and built-in tools."""

from __future__ import annotations

import pytest

from autobot.core.types import Risk
from autobot.tools.builtin import GET_TIME, get_time, register_builtins
from autobot.tools.registry import ToolRegistry, ToolSpec, default_registry


def test_get_time_returns_nonempty_string() -> None:
    assert isinstance(get_time(), str)
    assert get_time()


def test_default_registry_exposes_get_time_schema() -> None:
    registry = default_registry()
    names = [s["function"]["name"] for s in registry.schemas()]
    assert names == ["get_time"]


def test_dispatch_runs_registered_tool() -> None:
    registry = default_registry()
    result = registry.dispatch("get_time", {})
    assert result.ok is True
    assert result.name == "get_time"
    assert result.content


def test_dispatch_unknown_tool_is_failed_result_not_exception() -> None:
    result = ToolRegistry().dispatch("does_not_exist")
    assert result.ok is False
    assert "unknown tool" in result.content


def test_dispatch_captures_handler_errors() -> None:
    registry = ToolRegistry()

    def boom() -> str:
        raise RuntimeError("kaboom")

    registry.register(ToolSpec(name="boom", description="", parameters={}, handler=boom))
    result = registry.dispatch("boom")
    assert result.ok is False
    assert "kaboom" in result.content


def test_register_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(GET_TIME)


def test_builtin_tool_is_read_only() -> None:
    assert GET_TIME.risk is Risk.READ_ONLY


def test_toolspec_network_defaults_false() -> None:
    spec = ToolSpec(name="t", description="", parameters={}, handler=lambda: "")
    assert spec.network is False


def test_toolspec_network_can_be_set() -> None:
    spec = ToolSpec(name="t", description="", parameters={}, handler=lambda: "", network=True)
    assert spec.network is True


def _spec(name: str, desc: str = "") -> ToolSpec:
    return ToolSpec(name=name, description=desc, parameters={}, handler=lambda: name)


def test_register_duplicate_still_raises_by_default() -> None:
    registry = ToolRegistry()
    registry.register(_spec("dup"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("dup"))


def test_register_replace_overwrites_existing() -> None:
    registry = ToolRegistry()
    registry.register(_spec("t", "old"))
    registry.register(_spec("t", "new"), replace=True)
    spec = registry.get("t")
    assert spec is not None
    assert spec.description == "new"


def test_unregister_removes_tool_and_reports_existed() -> None:
    registry = ToolRegistry()
    registry.register(_spec("gone"))
    assert registry.unregister("gone") is True
    assert registry.get("gone") is None
    assert "gone" not in [s["function"]["name"] for s in registry.schemas()]


def test_unregister_missing_tool_returns_false() -> None:
    assert ToolRegistry().unregister("never") is False
