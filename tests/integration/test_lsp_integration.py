"""End-to-end LSP tests against a REAL language server (python-lsp-server).

Skipped unless ``pylsp`` is on PATH. These spawn an actual server and drive the real
``symbol`` / ``diagnostics`` / ``rename_symbol`` tools over a small project — the only way to
catch response-shape, timing, and cold-start bugs a fake transport can't. (Enable locally with
``uv pip install "python-lsp-server[rope]" pyflakes``; CI installs it via the dev extra.)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.diagnostics import _make_diag_fn, diagnostics
from autobot.tools.code.rename import _make_rename_fn, rename_symbol
from autobot.tools.code.symbol_nav import LspManager, symbol

pytestmark = pytest.mark.skipif(
    shutil.which("pylsp") is None, reason="pylsp language server not installed"
)


class _Confirmer:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self, prompt: str, options: object, kind: str = "read", default: str = "read"
    ) -> str:
        return "write"


def _broker(root: Path) -> AccessBroker:
    return AccessBroker(
        AccessPolicy(store_path=root / ".access.json", workspace_root=root), _Confirmer()
    )


def test_lsp_end_to_end_with_pylsp(tmp_path: Path) -> None:
    lib = tmp_path / "lib.py"
    lib.write_text("def greet(name):\n    return name\n\ndef caller():\n    return greet('x')\n")
    app = tmp_path / "app.py"
    app.write_text("from lib import greet\n\nundefined_thing()\nprint(greet('y'))\n")
    broker = _broker(tmp_path)
    mgr = LspManager()
    try:
        # go-to-definition: greet used at app.py:4 resolves to its definition at lib.py:1
        defn = symbol("definition", "greet", str(app), broker, line=4, manager=mgr)
        assert "language server" in defn and "lib.py:1" in defn

        # find-references: every use of greet, across both files
        refs = symbol("references", "greet", str(lib), broker, line=1, manager=mgr)
        assert "language server" in refs
        assert "lib.py:1" in refs and "app.py" in refs

        # hover: the signature
        hover = symbol("hover", "greet", str(lib), broker, line=1, manager=mgr)
        assert "greet" in hover

        # diagnostics: pyflakes flags the undefined name in app.py
        diags = diagnostics(str(app), broker, diag_fn=_make_diag_fn(mgr))
        assert "undefined" in diags.lower()

        # semantic rename across files (mutates — do it last)
        out = rename_symbol(
            "greet", str(lib), "welcome", broker, line=1, rename_fn=_make_rename_fn(mgr)
        )
        assert "welcome" in out
        assert "welcome" in lib.read_text() and "def greet" not in lib.read_text()
        assert "welcome" in app.read_text()
    finally:
        mgr.shutdown_all()
