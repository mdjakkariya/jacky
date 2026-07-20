"""Tests for rename_symbol — the WorkspaceEdit→files flow, driven by an injected rename_fn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.rename import register_rename_tool, rename_symbol
from autobot.tools.code.symbol_nav import LspManager
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


def _edit(sl: int, sc: int, el: int, ec: int, new: str) -> dict[str, Any]:
    return {
        "range": {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}},
        "newText": new,
    }


def test_rename_applies_workspace_edit_across_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("foo = 1\n")
    b = tmp_path / "b.py"
    b.write_text("print(foo)\n")

    def fake_rename(resolved: Path, line0: int, col: int, new_name: str) -> dict[str, Any]:
        return {
            "changes": {
                a.as_uri(): [_edit(0, 0, 0, 3, new_name)],  # foo -> bar
                b.as_uri(): [_edit(0, 6, 0, 9, new_name)],  # foo inside print(...)
            }
        }

    out = rename_symbol("foo", str(a), "bar", _broker(tmp_path), line=1, rename_fn=fake_rename)
    assert a.read_text() == "bar = 1\n"
    assert b.read_text() == "print(bar)\n"
    assert "2 file" in out and "bar" in out


def test_rename_declines_without_a_server(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("foo = 1\n")
    out = rename_symbol("foo", str(f), "bar", _broker(tmp_path), line=1, rename_fn=lambda *a: None)
    assert "language server" in out.lower()
    assert f.read_text() == "foo = 1\n"  # nothing changed


def test_rename_requires_line(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("foo = 1\n")
    out = rename_symbol("foo", str(f), "bar", _broker(tmp_path), line=0, rename_fn=lambda *a: {})
    assert "line" in out.lower()


def test_rename_name_not_on_line(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    out = rename_symbol("foo", str(f), "bar", _broker(tmp_path), line=1, rename_fn=lambda *a: {})
    assert "couldn't find" in out.lower()


def test_rename_missing_new_name(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("foo = 1\n")
    out = rename_symbol("foo", str(f), "", _broker(tmp_path), line=1, rename_fn=lambda *a: {})
    assert "new_name" in out.lower()


def test_rename_tool_registered_as_write(tmp_path: Path) -> None:
    reg = ToolRegistry()
    register_rename_tool(reg, _broker(tmp_path), LspManager())
    spec = reg.get("rename_symbol")
    assert spec is not None
    assert spec.risk == Risk.WRITE
