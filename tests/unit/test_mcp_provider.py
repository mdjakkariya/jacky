"""Unit tests for McpProvider — the lazy, runtime-toggleable MCP lifecycle holder."""

from __future__ import annotations

from typing import Any, cast

from autobot.mcp.provider import McpProvider


class _FakeManager:
    """Minimal stand-in for McpManager: records shutdown calls."""

    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


def _provider() -> tuple[McpProvider, list[Any]]:
    """A provider whose factory records each manager it builds."""
    built: list[Any] = []

    def factory() -> Any:
        mgr = _FakeManager()
        built.append(mgr)
        return cast("Any", mgr)

    return McpProvider(factory), built


def test_disabled_by_default() -> None:
    provider, built = _provider()
    assert provider.manager is None
    assert built == []


def test_enable_builds_and_exposes_manager() -> None:
    provider, built = _provider()
    provider.set_enabled(True)
    assert provider.manager is built[0]
    assert len(built) == 1


def test_enable_is_idempotent_no_second_build() -> None:
    provider, built = _provider()
    provider.set_enabled(True)
    provider.set_enabled(True)
    assert len(built) == 1  # factory not called again while already enabled


def test_disable_shuts_down_and_clears() -> None:
    provider, built = _provider()
    provider.set_enabled(True)
    mgr = built[0]
    provider.set_enabled(False)
    assert provider.manager is None
    assert mgr.shutdowns == 1


def test_disable_when_already_off_is_noop() -> None:
    provider, built = _provider()
    provider.set_enabled(False)
    assert provider.manager is None
    assert built == []


def test_re_enable_builds_a_fresh_manager() -> None:
    provider, built = _provider()
    provider.set_enabled(True)
    provider.set_enabled(False)
    provider.set_enabled(True)
    assert len(built) == 2
    assert provider.manager is built[1]


def test_shutdown_disables() -> None:
    provider, built = _provider()
    provider.set_enabled(True)
    mgr = built[0]
    provider.shutdown()
    assert provider.manager is None
    assert mgr.shutdowns == 1
