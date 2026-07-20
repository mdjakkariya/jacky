"""Tests for the diagnostics tool's formatting + dispatch (LSP call injected, no server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.diagnostics import diagnostics, register_diagnostics_tool
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


def test_diagnostics_formats_and_sorts(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    diags: list[dict[str, Any]] = [
        {"severity": 2, "message": "unused import", "range": {"start": {"line": 0}}},
        {
            "severity": 1,
            "message": "undefined name",
            "source": "pyright",
            "range": {"start": {"line": 2}},
        },
    ]
    out = diagnostics(str(f), _broker(tmp_path), diag_fn=lambda r, lang: diags)
    assert "2 problem" in out
    assert "[error]" in out and "[warning]" in out
    assert "a.py:3" in out  # 0-based line 2 -> shown as 3
    assert "pyright" in out
    assert out.index("[error]") < out.index("[warning]")  # errors first


def test_diagnostics_clean_file(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert (
        "no problems" in diagnostics(str(f), _broker(tmp_path), diag_fn=lambda r, lang: []).lower()
    )


def test_diagnostics_no_server_installed(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    out = diagnostics(str(f), _broker(tmp_path), diag_fn=lambda r, lang: None)
    assert "run_command" in out.lower()


def test_diagnostics_unsupported_language_declines(tmp_path: Path) -> None:
    f = tmp_path / "a.rb"  # no language server configured for Ruby
    f.write_text("x = 1\n")
    out = diagnostics(str(f), _broker(tmp_path), diag_fn=lambda r, lang: [{"message": "x"}])
    assert "no language server" in out.lower()


def test_diagnostics_missing_file(tmp_path: Path) -> None:
    out = diagnostics(str(tmp_path / "nope.py"), _broker(tmp_path), diag_fn=lambda r, lang: [])
    assert "no file" in out.lower()


def test_diagnostics_registered_read_only(tmp_path: Path) -> None:
    reg = ToolRegistry()
    register_diagnostics_tool(reg, _broker(tmp_path), LspManager())
    spec = reg.get("diagnostics")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY
