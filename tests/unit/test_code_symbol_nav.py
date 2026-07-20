"""Tests for the symbol tool's dispatch + textual fallback (no language server needed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.symbol_nav import (
    _column_of,
    _language_for,
    _server_argv,
    register_symbol_tool,
    symbol,
)
from autobot.tools.registry import ToolRegistry


class _FakeConfirmer:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default


def _broker(tmp_path: Path) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer())


def test_column_of_finds_word() -> None:
    assert _column_of("    return foo(bar)", "foo") == 11
    assert _column_of("foobar", "foo") is None  # word boundary — not a substring match
    assert _column_of("x = 1", "foo") is None


def test_language_for() -> None:
    assert _language_for("a.py") == "python"
    assert _language_for("a.rb") is None  # unsupported → will fall back


def test_server_argv_shape() -> None:
    # Whether or not a server is installed, this returns a candidate argv list or None.
    result = _server_argv("python")
    assert result is None or (isinstance(result, list) and result)
    assert _server_argv("cobol") is None  # unconfigured language


def _capture_grep(store: dict[str, Any]) -> Any:
    def fake_grep(
        pattern: str,
        broker: Any,
        path: str = ".",
        glob: Any = None,
        ignore_case: bool = False,
        output_mode: str = "files_with_matches",
        context: int = 0,
    ) -> str:
        store["pattern"] = pattern
        store["mode"] = output_mode
        return "GREP-OUTPUT"

    return fake_grep


def test_symbol_references_falls_back_to_word_boundary_grep(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("foo = 1\n")
    store: dict[str, Any] = {}
    out = symbol("references", "foo", str(f), _broker(tmp_path), grep=_capture_grep(store))
    assert "GREP-OUTPUT" in out and "fallback" in out.lower()
    assert store["pattern"] == r"\bfoo\b"
    assert store["mode"] == "content"


def test_symbol_definition_falls_back_to_definition_pattern(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n")
    store: dict[str, Any] = {}
    symbol("definition", "foo", str(f), _broker(tmp_path), grep=_capture_grep(store))
    assert "def" in store["pattern"] and "foo" in store["pattern"]  # definition-shaped


def test_symbol_rejects_bad_action(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x\n")
    assert "action" in symbol("wat", "foo", str(f), _broker(tmp_path)).lower()


def test_symbol_requires_name(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x\n")
    assert "name" in symbol("references", "", str(f), _broker(tmp_path)).lower()


def test_symbol_missing_file(tmp_path: Path) -> None:
    out = symbol("references", "foo", str(tmp_path / "nope.py"), _broker(tmp_path))
    assert "no file" in out.lower()


def test_symbol_registered_read_only(tmp_path: Path) -> None:
    reg = ToolRegistry()
    register_symbol_tool(reg, _broker(tmp_path))
    spec = reg.get("symbol")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY
